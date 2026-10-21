"""Shared LLM facade: stable task contract, bounded deterministic failover."""
import time
import requests  # Compatibility for existing offline patches of llm.requests.post.
from uuid import uuid4
import config
from llm_connections import DEFAULT_MODEL, load_connections, ConnectionConfigError
from llm_errors import LLMUnavailable, LLMStopped
from llm_provider import execute
from privacy import sanitize_candidate_text

RATE_RETRIES = 1
DEFAULT_RETRY_DELAY = 2.0
MAX_RETRY_DELAY = 30.0
TASKS = frozenset({'matching', 'cover_letter', 'question', 'text'})
RETRYABLE_CATEGORIES = frozenset({
    'rate_limit_requests', 'rate_limit_tokens', 'rate_limit_unknown',
    'provider_server_error', 'provider_transport_timeout', 'provider_transport_connection',
})
TRANSIENT_RATE_LIMIT_CATEGORIES = frozenset({
    'rate_limit_requests', 'rate_limit_tokens', 'rate_limit_unknown',
})
RUN_PERMANENT_CATEGORIES = frozenset({
    'quota_daily', 'auth/provider_access', 'connection_config_invalid',
})
ROUTER_ROUNDS = 2
RECOVERY_OPERATION_WAIT_LIMIT = 30.0
RECOVERY_RUN_WAIT_LIMIT = 120.0


def _failure_scope(failure):
    return ('run_permanent' if failure.category in RUN_PERMANENT_CATEGORIES
            else 'operation_scoped')


class Router:
    def __init__(self, connections, sleep=None, clock=None, telemetry=None,
                 operation_id_factory=None, stopped=None):
        self.connections = tuple(connections)
        self.unavailable = set()
        self.active = None
        self.failures = []
        self.routing_events = []
        self.sleep = sleep or time.sleep
        self.clock = clock or time.perf_counter
        self.telemetry = telemetry
        self.operation_id_factory = operation_id_factory or (lambda: uuid4().hex)
        self.stopped = stopped
        self.recovery_wait_used = 0.0

    def set_telemetry(self, telemetry):
        self.telemetry = telemetry

    def set_stopped(self, stopped):
        self.stopped = stopped

    def _check_stopped(self):
        if self.stopped is not None and self.stopped():
            raise LLMStopped()

    def _interruptible_sleep(self, delay):
        if self.stopped is None:
            self.sleep(delay)
            return
        remaining = delay
        while remaining > 0:
            self._check_stopped()
            interval = min(0.1, remaining)
            self.sleep(interval)
            remaining -= interval
        self._check_stopped()

    def _emit(self, event, **details):
        if self.telemetry is None:
            return
        try:
            self.telemetry({'event': event, **details})
        except Exception:
            # Diagnostics must never turn a successful provider request into failure.
            pass

    def _elapsed_ms(self, started):
        return max(0, round((self.clock() - started) * 1000))

    def _candidates(self):
        candidates = [c for c in self.connections if c.usable and c.id not in self.unavailable]
        if self.active is not None:
            same = [c for c in candidates if (c.provider, c.model) == (self.active.provider, self.active.model)]
            candidates = same + [c for c in candidates if c not in same]
            candidates.sort(key=lambda c: c.id != self.active.id)
        return candidates

    def _operation_candidates(self):
        """Freeze the existing dynamic priority as one deterministic round order."""
        remaining = self._candidates()
        order = []
        active = self.active
        while remaining:
            candidates = list(remaining)
            if active is not None:
                same = [c for c in candidates
                        if (c.provider, c.model) == (active.provider, active.model)]
                candidates = same + [c for c in candidates if c not in same]
                candidates.sort(key=lambda c: c.id != active.id)
            connection = candidates[0]
            order.append(connection)
            remaining.remove(connection)
            active = connection
        return tuple(order)

    def generate(self, prompt, max_tokens=600, task='text'):
        if task not in TASKS:
            raise ValueError('llm_task_unsupported')
        self._check_stopped()
        operation_id = self.operation_id_factory()
        operation_clock = self.clock()
        operation_start = len(self.failures)
        attempt_count = 0
        final_connection_id = None
        final_failure = None
        rounds_started = 0
        round_two_ready_at = {}
        round_two_excluded = set()
        payload_rejected_connections = set()
        transient_ready_at = {}
        recovery_wait_used = 0.0
        recovery_cycle_attempted = False
        recovery_attempt_count = 0
        recovery_budget_exhausted = False
        exhaustion_emitted = False
        operation_finished = False

        def operation_event(outcome):
            nonlocal operation_finished
            if operation_finished:
                return
            operation_finished = True
            self._emit('llm_operation_finished', operation_id=operation_id, task=task,
                       duration_ms=self._elapsed_ms(operation_clock), outcome=outcome,
                       final_connection_id=final_connection_id,
                       total_provider_attempt_count=attempt_count,
                       router_rounds_started=rounds_started)

        self._emit('llm_operation_started', operation_id=operation_id, task=task,
                   operation_elapsed_ms=0)
        try:
            prompt = sanitize_candidate_text(
                prompt, tuple(c.credential for c in self.connections) + (config.ACCESS_TOKEN,))
            operation_order = self._operation_candidates()
            for router_round in range(1, ROUTER_ROUNDS + 2):
                self._check_stopped()
                recovery_cycle = router_round > ROUTER_ROUNDS
                if recovery_cycle:
                    candidates = [connection for connection in operation_order
                                  if connection.usable
                                  and connection.id not in self.unavailable
                                  and connection.id in transient_ready_at
                                  and connection.id not in payload_rejected_connections]
                    candidates.sort(key=lambda item: transient_ready_at[item.id])
                    if not candidates:
                        break
                    recovery_cycle_attempted = True
                    self._emit('llm_rate_limit_recovery_started',
                               operation_id=operation_id, task=task,
                               recovery_cycle=1,
                               eligible_connection_count=len(candidates),
                               operation_wait_budget_ms=round(
                                   RECOVERY_OPERATION_WAIT_LIMIT * 1000),
                               run_wait_budget_remaining_ms=round(max(
                                   0.0, RECOVERY_RUN_WAIT_LIMIT
                                   - self.recovery_wait_used) * 1000),
                               operation_elapsed_ms=self._elapsed_ms(operation_clock))
                else:
                    candidates = [connection for connection in operation_order
                                  if connection.usable
                                  and connection.id not in self.unavailable
                                  and not (router_round == 2
                                           and (connection.id in round_two_excluded
                                                or connection.id
                                                in payload_rejected_connections))]
                if router_round == 2:
                    for connection in operation_order:
                        if (connection.id in payload_rejected_connections
                                and connection.id not in self.unavailable):
                            self._emit('llm_router_round_connection_skipped',
                                       operation_id=operation_id, task=task,
                                       router_round=router_round,
                                       connection_id=connection.id,
                                       provider=connection.provider,
                                       model=connection.model,
                                       reason_category='identical_payload_request_too_large',
                                       operation_elapsed_ms=self._elapsed_ms(operation_clock))
                        elif (connection.id in round_two_excluded
                                and connection.id not in self.unavailable):
                            self._emit('llm_router_round_connection_skipped',
                                       operation_id=operation_id, task=task,
                                       router_round=router_round,
                                       connection_id=connection.id,
                                       provider=connection.provider,
                                       model=connection.model,
                                       reason_category='retry_delay_exceeds_bound',
                                       operation_elapsed_ms=self._elapsed_ms(operation_clock))
                if not candidates:
                    break
                if not recovery_cycle:
                    rounds_started = router_round
                if router_round == 2:
                    self._check_stopped()
                    self._emit('llm_router_round_started', operation_id=operation_id,
                               task=task, router_round=router_round,
                               eligible_connection_count=len(candidates),
                               operation_elapsed_ms=self._elapsed_ms(operation_clock))
                for connection_index, connection in enumerate(candidates):
                    self._check_stopped()
                    if recovery_cycle:
                        remaining_delay = max(0.0, transient_ready_at[connection.id]
                                              - self.clock())
                        operation_budget = max(
                            0.0, RECOVERY_OPERATION_WAIT_LIMIT - recovery_wait_used)
                        run_budget = max(
                            0.0, RECOVERY_RUN_WAIT_LIMIT - self.recovery_wait_used)
                        if remaining_delay > min(operation_budget, run_budget):
                            recovery_budget_exhausted = True
                            self._emit('llm_rate_limit_recovery_connection_skipped',
                                       operation_id=operation_id, task=task,
                                       recovery_cycle=1,
                                       connection_id=connection.id,
                                       provider=connection.provider, model=connection.model,
                                       reason_category='recovery_wait_budget_exhausted',
                                       required_delay_ms=round(remaining_delay * 1000),
                                       operation_wait_budget_remaining_ms=round(
                                           operation_budget * 1000),
                                       run_wait_budget_remaining_ms=round(run_budget * 1000),
                                       operation_elapsed_ms=self._elapsed_ms(operation_clock))
                            continue
                        if remaining_delay > 0:
                            wait_common = dict(
                                operation_id=operation_id, task=task,
                                recovery_cycle=1,
                                connection_id=connection.id,
                                provider=connection.provider, model=connection.model,
                                requested_delay_ms=round(remaining_delay * 1000),
                            )
                            self._emit('llm_rate_limit_recovery_wait_started', **wait_common,
                                       operation_elapsed_ms=self._elapsed_ms(operation_clock))
                            wait_clock = self.clock()
                            recovery_wait_used += remaining_delay
                            self.recovery_wait_used += remaining_delay
                            self._interruptible_sleep(remaining_delay)
                            self._emit('llm_rate_limit_recovery_wait_finished', **wait_common,
                                       actual_duration_ms=self._elapsed_ms(wait_clock),
                                       operation_wait_used_ms=round(
                                           recovery_wait_used * 1000),
                                       run_wait_used_ms=round(
                                           self.recovery_wait_used * 1000),
                                       operation_elapsed_ms=self._elapsed_ms(operation_clock))
                    elif router_round == 2 and connection.id in round_two_ready_at:
                        remaining_delay = max(0.0, round_two_ready_at[connection.id]
                                              - self.clock())
                        if remaining_delay > 0:
                            wait_common = dict(
                                operation_id=operation_id, task=task,
                                router_round=router_round,
                                connection_id=connection.id,
                                provider=connection.provider, model=connection.model,
                                requested_delay_ms=round(remaining_delay * 1000),
                            )
                            self._emit('llm_router_round_wait_started', **wait_common,
                                       operation_elapsed_ms=self._elapsed_ms(operation_clock))
                            wait_clock = self.clock()
                            self._interruptible_sleep(remaining_delay)
                            self._emit('llm_router_round_wait_finished', **wait_common,
                                       actual_duration_ms=self._elapsed_ms(wait_clock),
                                       operation_elapsed_ms=self._elapsed_ms(operation_clock))
                    final_connection_id = connection.id
                    self.active = connection
                    failure_scope = None
                    attempts_for_connection = 1 if recovery_cycle else RATE_RETRIES + 1
                    for attempt in range(attempts_for_connection):
                        self._check_stopped()
                        attempt_count += 1
                        if recovery_cycle:
                            recovery_attempt_count += 1
                        attempt_number = attempt + 1
                        common = dict(operation_id=operation_id, task=task,
                                      router_round=router_round,
                                      connection_id=connection.id,
                                      provider=connection.provider,
                                      model=connection.model, attempt=attempt_number)
                        if recovery_cycle:
                            common['recovery_cycle'] = 1
                        self._emit('llm_attempt_started', **common,
                                   operation_elapsed_ms=self._elapsed_ms(operation_clock))
                        attempt_clock = self.clock()
                        text, failure = execute(connection, prompt, max_tokens)
                        self._check_stopped()
                        attempt_duration = self._elapsed_ms(attempt_clock)
                        if failure is None:
                            self._emit('llm_attempt_finished', **common,
                                       duration_ms=attempt_duration,
                                       operation_elapsed_ms=self._elapsed_ms(operation_clock),
                                       outcome='success', provider_error_category=None,
                                       http_status=None, transport_phase=None,
                                       transport_failure_detail=None, failure_scope=None,
                                       retry_allowed=False, failover_follows=False,
                                       connection_unavailable_after_attempt=False)
                            safe_text = sanitize_candidate_text(
                                text, tuple(c.credential for c in self.connections))
                            recovered = self.failures[operation_start:]
                            if recovered:
                                if recovery_cycle:
                                    recovery = 'bounded_rate_limit_recovery'
                                elif router_round == 2:
                                    recovery = 'second_router_round'
                                elif all(item['connection_id'] == connection.id
                                         for item in recovered):
                                    recovery = 'same_connection_retry'
                                else:
                                    recovery = 'failover_connection'
                                self.routing_events.append({
                                    'event': 'llm_routing_recovered',
                                    'task': task,
                                    'recovery': recovery,
                                    'router_round': router_round,
                                    'final_connection_id': connection.id,
                                    'failures': recovered,
                                })
                            operation_event('success')
                            return safe_text
                        final_failure = failure
                        failure_scope = _failure_scope(failure)
                        run_permanent = failure_scope == 'run_permanent'
                        delay = (failure.retry_delay if failure.retry_delay is not None
                                 else DEFAULT_RETRY_DELAY)
                        if failure.category == 'request_too_large':
                            payload_rejected_connections.add(connection.id)
                        if (not run_permanent
                                and failure.category in TRANSIENT_RATE_LIMIT_CATEGORIES):
                            transient_ready_at[connection.id] = (
                                self.clock() + max(1.0, delay))
                        else:
                            transient_ready_at.pop(connection.id, None)
                        retry_scheduled = (not run_permanent
                                           and failure.category in RETRYABLE_CATEGORIES
                                           and attempt < RATE_RETRIES
                                           and delay <= MAX_RETRY_DELAY
                                           and not recovery_cycle)
                        remaining_this_round = [
                            candidate for candidate in candidates[connection_index + 1:]
                            if candidate.id not in self.unavailable
                        ]
                        next_round_possible = (router_round < ROUTER_ROUNDS and any(
                            candidate.id not in self.unavailable
                            and candidate.id not in payload_rejected_connections
                            and not (run_permanent and candidate.id == connection.id)
                            and not (candidate.id == connection.id
                                     and failure.category in RETRYABLE_CATEGORIES
                                     and delay > MAX_RETRY_DELAY)
                            for candidate in operation_order
                        )) or (router_round == ROUTER_ROUNDS
                               and failure.category in TRANSIENT_RATE_LIMIT_CATEGORIES
                               and max(0.0, transient_ready_at[connection.id]
                                       - self.clock()) <= min(
                                           RECOVERY_OPERATION_WAIT_LIMIT - recovery_wait_used,
                                           RECOVERY_RUN_WAIT_LIMIT
                                           - self.recovery_wait_used))
                        failover_follows = (not retry_scheduled
                                            and bool(remaining_this_round
                                                     or next_round_possible))
                        failure_record = dict(
                            connection_id=connection.id, provider=connection.provider,
                            task=task, router_round=router_round,
                            attempt=attempt_number, retry_scheduled=retry_scheduled,
                            failure_scope=failure_scope, **failure.public())
                        if recovery_cycle:
                            failure_record['recovery_cycle'] = 1
                        if retry_scheduled:
                            # Keep the diagnostic delay equal to the short wait actually used.
                            failure_record['retry_delay'] = max(1.0, delay)
                        self.failures.append(failure_record)
                        self._emit('llm_attempt_finished', **common,
                                   duration_ms=attempt_duration,
                                   operation_elapsed_ms=self._elapsed_ms(operation_clock),
                                   outcome='failure', provider_error_category=failure.category,
                                   http_status=failure.http_status,
                                   transport_phase=failure.transport_phase,
                                   transport_failure_detail=failure.transport_failure_detail,
                                   provider_error_type=failure.provider_error_type,
                                   provider_error_code=failure.provider_error_code,
                                   prompt_char_count=failure.prompt_char_count,
                                   estimated_input_tokens=failure.estimated_input_tokens,
                                   request_body_bytes=failure.request_body_bytes,
                                   requested_output_tokens=failure.requested_output_tokens,
                                   failure_scope=failure_scope,
                                   retry_allowed=retry_scheduled,
                                   failover_follows=failover_follows,
                                   connection_unavailable_after_attempt=(
                                       run_permanent and not retry_scheduled))
                        if not retry_scheduled:
                            break
                        self._check_stopped()
                        requested_delay = max(1.0, delay)
                        self._emit('llm_retry_wait_started', **common,
                                   requested_delay_ms=round(requested_delay * 1000),
                                   operation_elapsed_ms=self._elapsed_ms(operation_clock))
                        wait_clock = self.clock()
                        # Never retry earlier than a provider's long delay merely to cap our wait.
                        self._interruptible_sleep(requested_delay)
                        self._emit('llm_retry_wait_finished', **common,
                                   requested_delay_ms=round(requested_delay * 1000),
                                   actual_duration_ms=self._elapsed_ms(wait_clock),
                                   operation_elapsed_ms=self._elapsed_ms(operation_clock))
                    if failure_scope == 'run_permanent':
                        self.unavailable.add(connection.id)
                    elif (router_round == 1
                          and final_failure.category in RETRYABLE_CATEGORIES):
                        cooldown = (final_failure.retry_delay
                                    if final_failure.retry_delay is not None
                                    else DEFAULT_RETRY_DELAY)
                        if cooldown > MAX_RETRY_DELAY:
                            round_two_excluded.add(connection.id)
                        else:
                            round_two_ready_at[connection.id] = (
                                self.clock() + max(1.0, cooldown))
                    remaining_this_round = [
                        candidate for candidate in candidates[connection_index + 1:]
                        if candidate.id not in self.unavailable
                    ]
                    if remaining_this_round:
                        self._emit('llm_failover', operation_id=operation_id, task=task,
                                   router_round=router_round,
                                   previous_connection_id=connection.id,
                                   next_connection_id=remaining_this_round[0].id,
                                   reason_category=final_failure.category,
                                   failure_scope=failure_scope,
                                   operation_elapsed_ms=self._elapsed_ms(operation_clock))
            if not exhaustion_emitted:
                exhaustion_emitted = True
                if (recovery_attempt_count and final_failure
                        and final_failure.category in TRANSIENT_RATE_LIMIT_CATEGORIES):
                    exhaustion_reason = 'transient_rate_limit_recovery_exhausted'
                elif recovery_budget_exhausted:
                    exhaustion_reason = 'recovery_wait_budget_exhausted'
                else:
                    exhaustion_reason = (final_failure.category if final_failure
                                         else 'no_usable_connections')
                self._emit('llm_connections_exhausted', operation_id=operation_id,
                           task=task, router_round=rounds_started,
                           rounds_completed=rounds_started,
                           last_connection_id=final_connection_id,
                           reason_category=exhaustion_reason,
                           recovery_cycle_attempted=recovery_cycle_attempted,
                           recovery_attempt_count=recovery_attempt_count,
                           operation_recovery_wait_used_ms=round(
                               recovery_wait_used * 1000),
                           run_recovery_wait_used_ms=round(
                               self.recovery_wait_used * 1000),
                           temporary_rate_limited_connection_count=len([
                               item for item in transient_ready_at
                               if item not in self.unavailable]),
                           request_too_large_connection_count=len(
                               payload_rejected_connections),
                           operation_elapsed_ms=self._elapsed_ms(operation_clock))
            raise LLMUnavailable(self.failures)
        except LLMStopped:
            operation_event('user_stop')
            raise
        except LLMUnavailable:
            operation_event('unavailable')
            raise
        except ValueError:
            operation_event('deterministic_failure')
            raise
        except Exception:
            operation_event('other_failure')
            raise

    def consume_routing_events(self):
        events, self.routing_events = self.routing_events, []
        return events


_router = None


def reset_runtime():
    """A new run starts fresh; persisted enabled flags are never mutated by routing."""
    global _router
    _router = None


def start_runtime(telemetry=None, stopped=None):
    """Freeze the connection list at run start, before loading private task data."""
    global _router
    if _router is None:
        invalid = False
        try:
            connections = load_connections()
        except ConnectionConfigError:
            invalid = True
        if invalid:
            raise LLMUnavailable([{'provider_error_category': 'connection_config_invalid'}])
        _router = Router(connections, telemetry=telemetry, stopped=stopped)
    elif telemetry is not None:
        _router.set_telemetry(telemetry)
    if stopped is not None:
        _router.set_stopped(stopped)


def generate(prompt: str, max_tokens: int = 600, *, task='text') -> str:
    start_runtime()
    return _router.generate(prompt, max_tokens, task)


def consume_routing_events():
    """Return safe recovery metadata for the current run journal entry."""
    return [] if _router is None else _router.consume_routing_events()

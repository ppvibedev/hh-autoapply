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
RUN_PERMANENT_CATEGORIES = frozenset({
    'quota_daily', 'auth/provider_access', 'connection_config_invalid',
})
ROUTER_ROUNDS = 2


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
            for router_round in range(1, ROUTER_ROUNDS + 1):
                self._check_stopped()
                candidates = [connection for connection in operation_order
                              if connection.usable
                              and connection.id not in self.unavailable
                              and not (router_round == 2
                                       and connection.id in round_two_excluded)]
                if router_round == 2:
                    for connection in operation_order:
                        if (connection.id in round_two_excluded
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
                rounds_started = router_round
                if router_round == 2:
                    self._check_stopped()
                    self._emit('llm_router_round_started', operation_id=operation_id,
                               task=task, router_round=router_round,
                               eligible_connection_count=len(candidates),
                               operation_elapsed_ms=self._elapsed_ms(operation_clock))
                for connection_index, connection in enumerate(candidates):
                    self._check_stopped()
                    if router_round == 2 and connection.id in round_two_ready_at:
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
                    for attempt in range(RATE_RETRIES + 1):
                        self._check_stopped()
                        attempt_count += 1
                        attempt_number = attempt + 1
                        common = dict(operation_id=operation_id, task=task,
                                      router_round=router_round,
                                      connection_id=connection.id,
                                      provider=connection.provider,
                                      model=connection.model, attempt=attempt_number)
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
                                if router_round == 2:
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
                        retry_scheduled = (not run_permanent
                                           and failure.category in RETRYABLE_CATEGORIES
                                           and attempt < RATE_RETRIES
                                           and delay <= MAX_RETRY_DELAY)
                        remaining_this_round = [
                            candidate for candidate in candidates[connection_index + 1:]
                            if candidate.id not in self.unavailable
                        ]
                        next_round_possible = (router_round < ROUTER_ROUNDS and any(
                            candidate.id not in self.unavailable
                            and not (run_permanent and candidate.id == connection.id)
                            and not (candidate.id == connection.id
                                     and failure.category in RETRYABLE_CATEGORIES
                                     and delay > MAX_RETRY_DELAY)
                            for candidate in operation_order
                        ))
                        failover_follows = (not retry_scheduled
                                            and bool(remaining_this_round
                                                     or next_round_possible))
                        failure_record = dict(
                            connection_id=connection.id, provider=connection.provider,
                            task=task, router_round=router_round,
                            attempt=attempt_number, retry_scheduled=retry_scheduled,
                            failure_scope=failure_scope, **failure.public())
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
                self._emit('llm_connections_exhausted', operation_id=operation_id,
                           task=task, router_round=rounds_started,
                           rounds_completed=rounds_started,
                           last_connection_id=final_connection_id,
                           reason_category=(final_failure.category if final_failure
                                            else 'no_usable_connections'),
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

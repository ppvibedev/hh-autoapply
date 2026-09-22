"""Shared LLM facade: stable task contract, run-scoped deterministic failover."""
import time
import requests  # Compatibility for existing offline patches of llm.requests.post.
import config
from llm_connections import DEFAULT_MODEL, load_connections, ConnectionConfigError
from llm_errors import LLMUnavailable
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


class Router:
    def __init__(self, connections, sleep=None):
        self.connections = tuple(connections)
        self.unavailable = set()
        self.active = None
        self.failures = []
        self.routing_events = []
        self.sleep = sleep or time.sleep

    def generate(self, prompt, max_tokens=600, task='text'):
        if task not in TASKS:
            raise ValueError('llm_task_unsupported')
        prompt = sanitize_candidate_text(prompt, tuple(c.credential for c in self.connections) + (config.ACCESS_TOKEN,))
        operation_start = len(self.failures)
        while True:
            candidates = [c for c in self.connections if c.usable and c.id not in self.unavailable]
            if self.active is not None:
                same = [c for c in candidates if (c.provider, c.model) == (self.active.provider, self.active.model)]
                candidates = same + [c for c in candidates if c not in same]
                candidates.sort(key=lambda c: c.id != self.active.id)
            if not candidates:
                raise LLMUnavailable(self.failures)
            connection = candidates[0]
            self.active = connection
            for attempt in range(RATE_RETRIES + 1):
                text, failure = execute(connection, prompt, max_tokens)
                if failure is None:
                    safe_text = sanitize_candidate_text(text, tuple(c.credential for c in self.connections))
                    recovered = self.failures[operation_start:]
                    if recovered:
                        self.routing_events.append({
                            'event': 'llm_routing_recovered',
                            'task': task,
                            'recovery': ('same_connection_retry' if all(
                                item['connection_id'] == connection.id for item in recovered
                            ) else 'failover_connection'),
                            'final_connection_id': connection.id,
                            'failures': recovered,
                        })
                    return safe_text
                delay = failure.retry_delay if failure.retry_delay is not None else DEFAULT_RETRY_DELAY
                retry_scheduled = (failure.category in RETRYABLE_CATEGORIES and attempt < RATE_RETRIES
                                   and delay <= MAX_RETRY_DELAY)
                failure_record = dict(connection_id=connection.id, provider=connection.provider,
                    task=task, attempt=attempt + 1, retry_scheduled=retry_scheduled,
                    **failure.public())
                if retry_scheduled:
                    # Keep the diagnostic delay equal to the short wait actually used.
                    failure_record['retry_delay'] = max(1.0, delay)
                self.failures.append(failure_record)
                if not retry_scheduled:
                    break
                # Never retry earlier than a provider's long delay merely to cap our wait.
                self.sleep(max(1.0, delay))
            self.unavailable.add(connection.id)

    def consume_routing_events(self):
        events, self.routing_events = self.routing_events, []
        return events


_router = None


def reset_runtime():
    """A new run starts fresh; persisted enabled flags are never mutated by routing."""
    global _router
    _router = None


def start_runtime():
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
        _router = Router(connections)


def generate(prompt: str, max_tokens: int = 600, *, task='text') -> str:
    start_runtime()
    return _router.generate(prompt, max_tokens, task)


def consume_routing_events():
    """Return safe recovery metadata for the current run journal entry."""
    return [] if _router is None else _router.consume_routing_events()

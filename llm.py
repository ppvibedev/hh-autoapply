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


class Router:
    def __init__(self, connections, sleep=None):
        self.connections = tuple(connections)
        self.unavailable = set()
        self.active = None
        self.failures = []
        self.sleep = sleep or time.sleep

    def generate(self, prompt, max_tokens=600, task='text'):
        if task not in TASKS:
            raise ValueError('llm_task_unsupported')
        prompt = sanitize_candidate_text(prompt, tuple(c.credential for c in self.connections) + (config.ACCESS_TOKEN,))
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
                    return sanitize_candidate_text(text, tuple(c.credential for c in self.connections))
                self.failures.append(dict(connection_id=connection.id, provider=connection.provider,
                    task=task, attempt=attempt + 1, **failure.public()))
                if failure.category not in ('rate_limit_requests', 'rate_limit_tokens', 'rate_limit_unknown') or attempt == RATE_RETRIES:
                    break
                delay = failure.retry_delay if failure.retry_delay is not None else DEFAULT_RETRY_DELAY
                # Never retry earlier than a provider's long delay merely to cap our wait.
                if delay > MAX_RETRY_DELAY:
                    break
                self.sleep(max(1.0, delay))
            self.unavailable.add(connection.id)


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

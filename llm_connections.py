"""Local connection configuration. Runtime availability is deliberately elsewhere."""
from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re
import tempfile
import threading
from uuid import uuid4

import config

STORAGE_PATH = Path(__file__).resolve().parent / 'private' / 'llm_connections.json'
DEFAULT_MODEL = {
    'anthropic': 'claude-haiku-4-5-20251001', 'openai': 'gpt-4o-mini',
    'yandex': 'yandexgpt-lite', 'gemini': 'gemini-1.5-flash',
    'groq': 'openai/gpt-oss-120b', 'zai': 'glm-4.7-flash',
}
_LOCK = threading.RLock()
_KNOWN_CREDENTIALS = set()


class ConnectionConfigError(ValueError):
    def __init__(self):
        super().__init__('llm_connection_config_invalid')


@dataclass(frozen=True)
class LLMConnection:
    id: str
    provider: str
    model: str = field(repr=False)
    enabled: bool = True
    credential: str = field(default='', repr=False, compare=False)
    folder_id: str = field(default='', repr=False)

    def public(self, order=0):
        return dict(id=self.id, provider=self.provider, model=self.model,
                    enabled=self.enabled, order=order, credential_configured=bool(self.credential),
                    folder_id=self.folder_id)

    @property
    def usable(self):
        # All existing adapters implement the same text-in/text-out task contract.
        return (self.enabled and self.provider in DEFAULT_MODEL and bool(self.credential)
                and bool(self.model) and (self.provider != 'yandex' or bool(self.folder_id)))


def _validate(row):
    if not isinstance(row, dict):
        raise ConnectionConfigError()
    cid, provider, model = (row.get(k, '') for k in ('id', 'provider', 'model'))
    key, folder, enabled = row.get('credential', ''), row.get('folder_id', ''), row.get('enabled', True)
    if (not isinstance(cid, str) or not re.fullmatch(r'legacy|[0-9a-f]{32}', cid)
        or not isinstance(provider, str) or provider not in DEFAULT_MODEL
        or not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}', model)
        or not isinstance(key, str) or any(c in key for c in '\r\n\x00')
        or not isinstance(folder, str) or (folder and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', folder))
        or type(enabled) is not bool):
        raise ConnectionConfigError()
    return LLMConnection(cid, provider, model, enabled, key, folder)


def _check_secret_fields(connections):
    keys = [c.credential for c in connections if c.credential]
    for c in connections:
        if any(key in value for key in keys for value in (c.id, c.provider, c.model, c.folder_id)):
            raise ConnectionConfigError()


def _safe_path():
    path = Path(STORAGE_PATH)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ConnectionConfigError()
    return path


def legacy_connection(env=None):
    def get(name, default=''):
        return (env.get(name, default) if env is not None else getattr(config, name, default)).strip()
    provider = get('LLM_PROVIDER', 'anthropic')
    if provider == 'none':
        return []
    if provider not in DEFAULT_MODEL:
        return []
    return [_validate(dict(id='legacy', provider=provider,
        model=get('LLM_MODEL') or DEFAULT_MODEL[provider], credential=get('LLM_API_KEY'),
        folder_id=get('YANDEX_FOLDER_ID')))]


def load_connections(env=None):
    try:
        path = _safe_path()
        if not path.exists():
            connections = legacy_connection(env)
            _check_secret_fields(connections)
            _KNOWN_CREDENTIALS.update(c.credential for c in connections if c.credential)
            return connections
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('connections'), list):
            raise ConnectionConfigError()
        connections = [_validate(row) for row in data['connections']]
        _check_secret_fields(connections)
        if len({c.id for c in connections}) != len(connections):
            raise ConnectionConfigError()
        _KNOWN_CREDENTIALS.update(c.credential for c in connections if c.credential)
        return connections
    except (OSError, ValueError, TypeError):
        raise ConnectionConfigError() from None


def credentials():
    # Preserve known keys if storage becomes unreadable. Loading the router itself
    # still fails closed; diagnostics must remain writable without reading bad JSON.
    try:
        load_connections()
    except ConnectionConfigError:
        pass
    return tuple(_KNOWN_CREDENTIALS) + (config.LLM_API_KEY, config.ACCESS_TOKEN)


def public_connections(connections):
    # Reject secrets entered into metadata fields, including another connection's key.
    from privacy import sanitize_candidate_text
    _check_secret_fields(connections)
    forbidden = tuple(c.credential for c in connections if c.credential)
    result = []
    for order, connection in enumerate(connections):
        row = connection.public(order)
        for name in ('id', 'provider', 'model', 'folder_id'):
            value = row[name]
            try:
                if any(key in value for key in forbidden) or sanitize_candidate_text(value, forbidden) != value:
                    raise ConnectionConfigError()
            except ValueError:
                raise ConnectionConfigError() from None
        result.append(row)
    return result


def save_change(payload, env=None, connection_id=None):
    """Add or update one connection; stable array order, atomic owner-only storage."""
    if not isinstance(payload, dict) or set(payload) - {'provider', 'model', 'credential', 'folder_id', 'enabled'}:
        raise ConnectionConfigError()
    with _LOCK:
        connections = load_connections(env)
        if connection_id is None:
            row = dict(id=uuid4().hex, enabled=True, credential='', folder_id='')
        else:
            previous = next((c for c in connections if c.id == connection_id), None)
            if previous is None:
                raise ConnectionConfigError()
            row = dict(id=previous.id, provider=previous.provider, model=previous.model,
                       credential=previous.credential, folder_id=previous.folder_id, enabled=previous.enabled)
        for name, value in payload.items():
            if name == 'credential' and value == '':
                continue  # Empty replacement preserves a saved key.
            row[name] = value
        if not row.get('model'):
            provider = row.get('provider')
            row['model'] = DEFAULT_MODEL.get(provider, '') if isinstance(provider, str) else ''
        connection = _validate(row)
        if connection_id is None:
            connections.append(connection)
        else:
            connections = [connection if c.id == connection_id else c for c in connections]
        public_connections(connections)
        path = _safe_path()
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix='.llm-connections-', dir=path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({'version': 1, 'connections': [dict(
                    id=c.id, provider=c.provider, model=c.model, enabled=c.enabled,
                    credential=c.credential, folder_id=c.folder_id) for c in connections]}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            raise ConnectionConfigError() from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        return public_connections(connections)

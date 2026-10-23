"""Existing provider HTTP contracts, isolated from routing and task validation."""
import errno
import json
import socket
import ssl

import requests
from urllib3 import exceptions as urllib3_exceptions
from llm_errors import ProviderFailure, classify_http


OPENAI_COMPATIBLE = {
    'openai': {
        'url': 'https://api.openai.com/v1/chat/completions',
        'extra': {},
        'token_limit_field': 'max_tokens',
    },
    'groq': {
        'url': 'https://api.groq.com/openai/v1/chat/completions',
        # GPT-OSS uses reasoning even for short tasks. Exclude it from the
        # response; callers consume only the final message content.
        'extra': {'reasoning_effort': 'low', 'include_reasoning': False},
        'token_limit_field': 'max_completion_tokens',
    },
    'zai': {
        'url': 'https://api.z.ai/api/paas/v4/chat/completions',
        # The selected GLM model supports disabling thinking; callers need only
        # the final answer text and already validate JSON/evidence themselves.
        'extra': {'thinking': {'type': 'disabled'}},
        'token_limit_field': 'max_tokens',
    },
}


def _exception_chain(error):
    """Yield exception objects only; arbitrary args/messages are never inspected."""
    pending = [error]
    seen = set()
    while pending and len(seen) < 24:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for name in ('__cause__', '__context__', 'reason', 'original_error', '_reason'):
            try:
                nested = getattr(current, name, None)
            except Exception:
                continue
            if isinstance(nested, BaseException):
                pending.append(nested)
        for nested in getattr(current, 'args', ()):
            if isinstance(nested, BaseException):
                pending.append(nested)


def classify_transport_failure(error):
    """Return a closed, privacy-safe subtype based only on types and errno."""
    chain = tuple(_exception_chain(error))

    def has(*types):
        supported = tuple(item for item in types if isinstance(item, type))
        return bool(supported) and any(isinstance(item, supported) for item in chain)

    proxy_error = getattr(urllib3_exceptions, 'ProxyError', None)
    resolution_error = getattr(urllib3_exceptions, 'NameResolutionError', None)
    urllib3_ssl_error = getattr(urllib3_exceptions, 'SSLError', None)
    connect_timeout = getattr(urllib3_exceptions, 'ConnectTimeoutError', None)
    new_connection = getattr(urllib3_exceptions, 'NewConnectionError', None)
    read_timeout = getattr(urllib3_exceptions, 'ReadTimeoutError', None)

    if has(requests.exceptions.ProxyError, proxy_error):
        return 'proxy'
    if has(socket.gaierror, resolution_error):
        return 'dns'
    if has(requests.exceptions.SSLError, ssl.SSLError, urllib3_ssl_error):
        return 'tls'

    error_numbers = {item.errno for item in chain
                     if isinstance(item, OSError) and isinstance(item.errno, int)}
    if has(ConnectionRefusedError) or errno.ECONNREFUSED in error_numbers:
        return 'tcp_refused'
    if has(ConnectionResetError) or errno.ECONNRESET in error_numbers:
        return 'tcp_reset'
    if error_numbers & {errno.ENETUNREACH, errno.EHOSTUNREACH}:
        return 'network_unreachable'

    if has(requests.ConnectTimeout):
        return 'connect_timeout'
    if connect_timeout and any(isinstance(item, connect_timeout)
                               and not (new_connection and isinstance(item, new_connection))
                               for item in chain):
        return 'connect_timeout'
    if has(requests.ReadTimeout, read_timeout):
        return 'read_timeout'
    if isinstance(error, requests.Timeout):
        return 'unknown_timeout'
    if isinstance(error, requests.ConnectionError):
        return 'unknown_connection'
    return 'unknown_transport'


def _openai_payload(connection, prompt, max_tokens):
    settings = OPENAI_COMPATIBLE[connection.provider]
    return {
        'model': connection.model,
        settings['token_limit_field']: max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
        **settings['extra'],
    }


def _groq_request_body(connection, prompt, max_tokens):
    """Serialize Groq JSON as compact UTF-8 without changing its JSON value."""
    if connection.provider != 'groq':
        return None
    payload = _openai_payload(connection, prompt, max_tokens)
    return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _groq_request_metadata(connection, prompt, max_tokens, body=None):
    """Return size counters only; prompt and serialized body are never retained."""
    if connection.provider != 'groq':
        return {}
    if body is None:
        body = _groq_request_body(connection, prompt, max_tokens)
    prompt_bytes = len(prompt.encode('utf-8'))
    return {
        'prompt_char_count': len(prompt),
        # A deliberately approximate, tokenizer-free diagnostic.
        'estimated_input_tokens': max(1, (prompt_bytes + 3) // 4),
        'request_body_bytes': len(body),
        'requested_output_tokens': max_tokens,
    }


def _openai_compatible(connection, prompt, max_tokens, request_body=None, timeout=60):
    settings = OPENAI_COMPATIBLE[connection.provider]
    payload = _openai_payload(connection, prompt, max_tokens)
    request = {
        'headers': {'Authorization': f'Bearer {connection.credential}',
                    'Content-Type': 'application/json'},
        'timeout': timeout,
    }
    if connection.provider == 'groq':
        request['data'] = request_body if request_body is not None else _groq_request_body(
            connection, prompt, max_tokens)
    else:
        request['json'] = payload
    response = requests.post(settings['url'], **request)
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content'].strip()


def execute(connection, prompt, max_tokens, *, timeout=60):
    failure = None
    request_body = _groq_request_body(connection, prompt, max_tokens)
    request_metadata = _groq_request_metadata(
        connection, prompt, max_tokens, body=request_body)
    try:
        text = _request(connection, prompt, max_tokens, request_body=request_body, timeout=timeout)
        if not isinstance(text, str) or not text.strip():
            return None, ProviderFailure('provider_invalid_response')
        return text, None
    except requests.HTTPError as error:
        failure = (classify_http(connection.provider, error.response, request_metadata)
                   if error.response is not None else ProviderFailure(
                       'provider_http_error', **request_metadata))
    except requests.ConnectTimeout as error:
        failure = ProviderFailure('provider_transport_timeout', transport_phase='connect',
                                  transport_failure_detail=classify_transport_failure(error))
    except requests.ReadTimeout as error:
        failure = ProviderFailure('provider_transport_timeout', transport_phase='read',
                                  transport_failure_detail=classify_transport_failure(error))
    except requests.Timeout as error:
        failure = ProviderFailure('provider_transport_timeout', transport_phase='generic',
                                  transport_failure_detail=classify_transport_failure(error))
    except requests.ConnectionError as error:
        failure = ProviderFailure('provider_transport_connection', transport_phase='connection',
                                  transport_failure_detail=classify_transport_failure(error))
    except requests.RequestException as error:
        failure = ProviderFailure('provider_transport_error',
                                  transport_failure_detail=classify_transport_failure(error))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        failure = ProviderFailure('provider_invalid_response')
    # Return outside except; no raw HTTP exception is attached to a later terminal error.
    return None, failure


def _request(connection, prompt, max_tokens, request_body=None, timeout=60):
    prov, key, model = connection.provider, connection.credential, connection.model
    if prov == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()

    if prov in OPENAI_COMPATIBLE:
        return _openai_compatible(connection, prompt, max_tokens, request_body=request_body, timeout=timeout)

    if prov == "yandex":
        folder = connection.folder_id
        model_name = connection.model
        r = requests.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={"Authorization": f"Api-Key {key}", "Content-Type": "application/json"},
            json={
                "modelUri": f"gpt://{folder}/{model_name}/latest",
                "completionOptions": {"stream": False, "temperature": 0.4, "maxTokens": str(max_tokens)},
                "messages": [{"role": "user", "text": prompt}],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["result"]["alternatives"][0]["message"]["text"].strip()

    if prov == "gemini":
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key},
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    return None

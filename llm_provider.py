"""Existing provider HTTP contracts, isolated from routing and task validation."""
import requests
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


def _openai_compatible(connection, prompt, max_tokens):
    settings = OPENAI_COMPATIBLE[connection.provider]
    payload = {
        'model': connection.model,
        settings['token_limit_field']: max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
        **settings['extra'],
    }
    response = requests.post(
        settings['url'],
        headers={'Authorization': f'Bearer {connection.credential}',
                 'Content-Type': 'application/json'},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content'].strip()


def execute(connection, prompt, max_tokens):
    failure = None
    try:
        text = _request(connection, prompt, max_tokens)
        if not isinstance(text, str) or not text.strip():
            return None, ProviderFailure('provider_invalid_response')
        return text, None
    except requests.HTTPError as error:
        failure = classify_http(connection.provider, error.response) if error.response is not None else ProviderFailure('provider_http_error')
    except requests.ConnectTimeout:
        failure = ProviderFailure('provider_transport_timeout', transport_phase='connect')
    except requests.ReadTimeout:
        failure = ProviderFailure('provider_transport_timeout', transport_phase='read')
    except requests.Timeout:
        failure = ProviderFailure('provider_transport_timeout', transport_phase='generic')
    except requests.ConnectionError:
        failure = ProviderFailure('provider_transport_connection', transport_phase='connection')
    except requests.RequestException:
        failure = ProviderFailure('provider_transport_error')
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        failure = ProviderFailure('provider_invalid_response')
    # Return outside except; no raw HTTP exception is attached to a later terminal error.
    return None, failure


def _request(connection, prompt, max_tokens):
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
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()

    if prov in OPENAI_COMPATIBLE:
        return _openai_compatible(connection, prompt, max_tokens)

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
            timeout=60,
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
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    return None

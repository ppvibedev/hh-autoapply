"""Клиент ИИ для генерации писем и оценки. Claude / OpenAI / YandexGPT / Gemini по ключу.

Ключ и провайдер — в .env (LLM_PROVIDER, LLM_API_KEY, LLM_MODEL). Без SDK,
чтобы не тянуть зависимости. Если ключа нет или провайдер none — вернёт None,
и вызывающий код откатится на шаблон.
"""
from __future__ import annotations

import requests

import config

DEFAULT_MODEL = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "yandex": "yandexgpt-lite",
    "gemini": "gemini-1.5-flash",
}


def generate(prompt: str, max_tokens: int = 600) -> str | None:
    prov = config.LLM_PROVIDER
    key = config.LLM_API_KEY
    if prov == "none" or not key:
        return None
    model = config.LLM_MODEL or DEFAULT_MODEL.get(prov, "")

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

    if prov == "openai":
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    if prov == "yandex":
        folder = config.YANDEX_FOLDER_ID
        model_name = config.LLM_MODEL or DEFAULT_MODEL["yandex"]
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

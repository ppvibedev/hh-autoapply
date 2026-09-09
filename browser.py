"""Управление браузером Playwright и сохранённой сессией hh.ru."""
import os
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

import config


@contextmanager
def browser_context(*, use_session: bool = True, on_stage=None):
    """Открывает контекст браузера. Если есть сохранённая сессия — грузит её.

    yield -> (context, page). По выходу всё закрывается.
    """
    if on_stage:
        on_stage("browser_init")
    with sync_playwright() as p:
        launch_args = {"headless": config.HEADLESS}
        browser = p.chromium.launch(**launch_args)

        ctx_args = {"user_agent": None, "locale": "ru-RU", "service_workers": "block"}
        if use_session and os.path.exists(config.SESSION_FILE):
            ctx_args["storage_state"] = config.SESSION_FILE
        else:
            ctx_args.pop("storage_state", None)

        if on_stage:
            on_stage("browser_context_init")
        context = browser.new_context(**{k: v for k, v in ctx_args.items() if v is not None})
        page = context.new_page()
        try:
            yield context, page
        finally:
            context.close()
            browser.close()


def has_session() -> bool:
    return os.path.exists(config.SESSION_FILE)

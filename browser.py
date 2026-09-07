"""Управление браузером Playwright и сохранённой сессией hh.ru."""
import os
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

import config


@contextmanager
def browser_context(*, use_session: bool = True):
    """Открывает контекст браузера. Если есть сохранённая сессия — грузит её.

    yield -> (context, page). По выходу всё закрывается.
    """
    with sync_playwright() as p:
        launch_args = {"headless": config.HEADLESS}
        browser = p.chromium.launch(**launch_args)

        ctx_args = {"user_agent": None, "locale": "ru-RU"}
        if use_session and os.path.exists(config.SESSION_FILE):
            ctx_args["storage_state"] = config.SESSION_FILE
        else:
            ctx_args.pop("storage_state", None)

        context = browser.new_context(**{k: v for k, v in ctx_args.items() if v is not None})
        page = context.new_page()
        try:
            yield context, page
        finally:
            context.close()
            browser.close()


def has_session() -> bool:
    return os.path.exists(config.SESSION_FILE)

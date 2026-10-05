# HH Auto Apply

Локальный dashboard для поиска вакансий на hh.ru, LLM matching и контролируемой
подготовки/отправки откликов. Текущий production path работает через браузер Playwright
под сохранённой HH-сессией.

## SAFE и LIVE

- **SAFE** анализирует вакансии и готовит письма без отправки откликов.
- **LIVE** разрешает реальные отклики, но каждый новый run требует отдельного явного
  подтверждения. Сохранённый LIVE сам ничего не запускает.

## Быстрый старт

Нужен нативный Python 3.11 и Chromium для Playwright.

```bash
bash setup.sh
bash start.sh
```

Откройте `http://127.0.0.1:8000`, выполните ручной вход в HH, обновите список резюме,
выберите exact резюме, настройте LLM connections и сначала выполните SAFE-run.

Настройки хранятся локально в `.env`; безопасный шаблон — `.env.example`.

## Документация

- [Логика продукта — CURRENT canonical behavior](docs/PRODUCT_LOGIC.md)
- [Установка, запуск и troubleshooting](docs/OPERATIONS.md)
- [Техническая карта](docs/TECHNICAL.md)
- [Индекс и исторические материалы](docs/README.md)

## Безопасность

LIVE взаимодействует с реальным аккаунтом HH. Не используйте реальные отклики как тест.
`applied_confirmed` допустим только после явного подтверждения HH; `submission_unknown`
требует ручной проверки и блокирует слепой повтор.

LLM получает sanitized профессиональный контекст и текст вакансии. Не публикуйте `.env`,
`state/`, `private/`, HH session/cookies, API keys, contacts, resume cache и runtime logs.
Dashboard слушает только localhost.

Автоматизация UI может противоречить правилам hh.ru и несёт риск для аккаунта. CAPTCHA
не обходится автоматически.

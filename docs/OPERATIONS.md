# HH Auto Apply — эксплуатация

**Status:** CURRENT operational guide. Product semantics находятся в
[PRODUCT_LOGIC.md](PRODUCT_LOGIC.md).

## Требования

- macOS;
- нативный Python 3.11 рекомендуется и является проверенной версией проекта;
- виртуальное окружение `.venv`;
- Chromium, установленный Playwright;
- доступ к локальному адресу `http://127.0.0.1:8000`.

`setup.sh` технически принимает любой доступный `python3`, но для воспроизводимости
используйте Python 3.11 той же архитектуры, что и Mac. После обновления macOS старое
x86_64-окружение может потребовать пересоздания на arm64 Python.

## Установка окружения

```bash
bash setup.sh
```

Скрипт создаёт `.venv`, устанавливает только `requirements.txt`, ставит Chromium и при
отсутствии `.env` копирует `.env.example`. Альтернатива вручную:

```bash
/usr/local/bin/python3.11 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/playwright install chromium
cp .env.example .env
```

## Запуск

```bash
bash start.sh
```

Dashboard: `http://127.0.0.1:8000`. На macOS можно открыть `hh-autoapply.command`.
Это запускает dashboard, но само по себе не запускает run и не отправляет отклики.

## HH login и session

В dashboard нажмите «Открыть окно входа», войдите в HH вручную и сохраните сессию.
Пароль приложение не читает. Browser session хранится локально в
`state/session.json`; это приватный файл, его нельзя коммитить или переносить в GitHub.
После переноса на другой Mac безопаснее выполнить вход заново.

## Выбранное резюме

Нажмите «Обновить список резюме», затем выберите exact резюме в dashboard. Каталог
хранится в `state/resumes.json`, тексты — в `state/resumes/`. Перед каждым run выбранное
резюме обновляется один раз и становится immutable snapshot этого run.

## Telegram для откликов

В dashboard откройте настройки **Telegram для откликов**, введите значение в формате
`@username` и нажмите **Сохранить**. Значение хранится локально, используется только для
ответов на Telegram-вопросы работодателей и не передаётся в ИИ. Его можно изменить или
очистить тем же полем.

## SAFE/LIVE workflow

1. Настройте search, selected resume и LLM connections.
2. Первый прогон выполняйте в SAFE.
3. Проверьте matching, manual outcomes и письма.
4. Для LIVE сохраните `RUN_MODE=LIVE`.
5. Каждый LIVE-run отдельно подтвердите при нажатии запуска.
6. Сверяйте `submission_unknown` вручную в HH; не повторяйте такую вакансию вслепую.

Сохранённый LIVE не запускает worker автоматически. SAFE/LIVE подробно описаны в
[PRODUCT_LOGIC.md](PRODUCT_LOGIC.md#2-safe-и-live).

## LLM connections

Dashboard управляет несколькими локальными connections: provider, model, credential,
folder ID (для Yandex) и enabled state. Конфигурация хранится в
`private/llm_connections.json`; ключи не показываются обратно в UI и не должны попадать
в логи/Git. Поддержаны Anthropic, OpenAI, Yandex, Gemini, Groq и Z.AI.

LLM получает sanitized профессиональные данные, vacancy и task context. Это внешний
сетевой вызов, поэтому нельзя описывать приложение как полностью локальное.

## Настройки

Активные настройки находятся в `.env` и редактируются dashboard. Безопасный шаблон —
`.env.example`. Основные группы: режим и лимит, search/area/salary, HH filters,
stopwords/blacklist, threshold, selected resume, browser pages/pause/headless и legacy
LLM connection.

`AREA=` — весь мир. `PAGES_PER_RUN` — бюджет страниц. `TARGET_APPLICATIONS_PER_RUN` —
число явно подтверждённых новых откликов, после которого run останавливается.

## Локальные и приватные файлы

Никогда не публикуйте:

- `.env`;
- `state/` (session, history, resume cache, safety ledger, pid/socket);
- `private/` (candidate context, preferences, LLM connections);
- cookies, tokens, contacts, API keys и credentials;
- `.venv/`, логи и временные LIVE diagnostics.

`.gitignore` не заменяет проверку фактического Git index перед commit.

## История и Stop

Главный экран после перезапуска dashboard пуст, пока не начат новый run. Persistent
history остаётся на диске. Stop — кооперативный: UI подтверждает запрос, а окончательное
завершение подтверждает status run. Уже начатый browser/LLM вызов может ждать timeout.

Кнопка «Сбросить историю» не удаляет HH session и application safety ledger. Для полного
удаления приватного локального состояния есть `cleanup.sh`; он удаляет `.env` и `state/`,
поэтому запускайте его только когда действительно хотите потерять эти данные.

## Offline tests

Полный локальный набор:

```bash
./.venv/bin/python -m unittest discover -s tests
```

Тесты используют synthetic data и блокируют внешнюю сеть. Некоторые lifecycle tests
создают временный Unix control socket; ограниченная sandbox-среда может запретить это,
что следует отличать от regression production-кода.

## Troubleshooting

### `Bad CPU type in executable`

Архитектура Python внутри `.venv` не совпадает с архитектурой Mac. Убедитесь, что
подходящий нативный Python 3.11 уже установлен, затем пересоздайте только `.venv` и
переустановите `requirements.txt`/Chromium. Не трогайте `.env`, `state/` и `private/`.

### Dashboard сообщает, что session отсутствует

Снова выполните ручной HH login. Не копируйте cookies в логи или issue.

### `submission_unknown`

Проверьте кабинет HH вручную. Persistent safety специально блокирует автоматический
повтор, потому что request мог быть отправлен.

### `no_results_or_page_not_recognized`

Это не доказанный «конец выдачи». Смотрите `search_page_diagnostic`: login, CAPTCHA,
HTTP error, raw cards/parser или 0/0 unknown state.

### CAPTCHA

Приложение не обходит CAPTCHA. Когда HH её показывает, run автоматически ставится
на паузу. Перейдите в открытое окно Chromium и решите CAPTCHA вручную; больше
ничего нажимать не нужно. Run сам возобновит ту же операцию. Если не решить её
за 120 секунд, run остановится с `hh_captcha_timeout`. Во время ожидания можно нажать Stop.

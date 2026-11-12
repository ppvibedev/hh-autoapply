# HH Auto Apply — техническая карта

**Status:** CURRENT architecture summary. Наблюдаемая продуктовая семантика описана в
[PRODUCT_LOGIC.md](PRODUCT_LOGIC.md).

## Production entry points

- `hh-autoapply.command` / `start.sh` → `webapp.py` → локальный dashboard.
- `webapp.py` запускает `run_web.py` отдельным процессом.
- `run_web.py` оркестрирует browser-first pipeline.
- `hh_web.py` отвечает за search/detail и передаёт application в `application.py`.
- `browser.py`, `login_web.py`, `list_resumes.py` управляют Playwright context/session.

`run.py` + `hh_client.py` — отдельный legacy API path. Он не является текущим
production application flow и имеет собственные ограничения HH API.

## Module map

| Модуль | Ответственность |
|---|---|
| `config.py`, `envfile.py` | загрузка/сохранение разрешённых настроек |
| `decide.py` | hard filters, matching contract, cover letter |
| `llm.py` | router, bounded retry/failover, task telemetry |
| `llm_connections.py` | приватная конфигурация connections |
| `llm_provider.py` | provider transports и нормализация failures |
| `candidate_context.py`, `privacy.py` | локальные материалы и privacy boundary |
| `resume_metadata.py` | atomic metadata catalog, exact-ID lookup |
| `application_profile.py`, `question_sources.py` | приватный application contact и локальные resolvers/preference routing |
| `application.py` | response state machine, questions, resume, submit confirmation |
| `history_store.py` | JSONL locking/retention и application safety ledger |
| `webapp.py`, `web/index.html` | API/dashboard/current-run UI/history paging/Stop |

## State и private files

`state/` содержит HH session, resume metadata/cache, decisions journal, application safety,
worker state и control socket. `private/` содержит candidate context, preferences, локальный
`application_profile.json` и LLM connections. Оба каталога runtime-private и игнорируются Git.

`state/decisions.jsonl` — append-only operational journal с retention: пять последних
завершённых runs плюс активный. `state/application_safety.json` — компактный exact vacancy
ID ledger для `applied_confirmed`, `already_applied`, `submission_unknown`; pruning history
его не стирает.

## Run snapshot и identifiers

Каждый run получает UUID `run_id`. Source of truth для resume — exact ID из `RESUME_URL`.
Title извлекается из атомарного `resumes.json`, после чего текст exact resume обновляется
один раз. Search settings и resume ID пишутся в `run_started` как безопасный snapshot.

## Control channel

Worker создаёт private Unix socket и `state/run.pid` v2 с PID/run ID/control token. Token
не попадает в diagnostics. Same-dashboard и recovered Stop сначала используют
authenticated socket; для same-dashboard остаётся fallback через process handle.
Correlation выполняется через `stop_attempt_id`. Heartbeat каждые 10 секунд фиксирует
этап и vacancy ID; после 180 секунд без прогресса watchdog пишет `operation_timeout`
и запрашивает кооперативный Stop. Он не убивает начатый Playwright-вызов: перед ручной
эскалацией необходимо проверить submit/safety ledger.
Stage `application_route_setup` отделяет регистрацию Playwright request guard от
`application_submit` и `application_confirm`; это позволяет отличить зависание до
отправки от ожидания после потенциального побочного эффекта.

Доказанная HH CAPTCHA на search/detail/application переводит worker в общий
`captcha_wait`. События содержат только run ID, vacancy ID и безопасный URL path.
Worker остаётся на текущем контексте до подтверждённого автоматического решения либо
authenticated `captcha_continue` с проверкой ожидаемой структуры HH.
`CAPTCHA_REMINDER_SECONDS` пишет событие истечения срока,
но не закрывает браузер. После потенциального submit сохраняется `submission_unknown`
и submit не повторяется.

`anti_captcha.py` классифицирует видимые iframe-маркеры по заранее разрешённым
host/path префиксам и полный структурный набор графической CAPTCHA в видимом модальном
окне без чтения изображения, текста или значений полей при классификации. Worker вызывает
пассивный снимок при входе в `captcha_wait`; тип и ограниченные счётчики сохраняются в `captcha_wait_started` и
показываются в dashboard. При включённой графической интеграции worker вызывает
`getBalance`, `createTask` с типом `ImageToTextTask` и `getTaskResult` с ограниченным
Stop-aware polling. Изображение берётся только из единственного CAPTCHA-окна; ответ
не попадает в telemetry. Потокобезопасный реестр HMAC изображения в памяти worker
не допускает одновременные и повторные задачи для той же картинки; после обновления
изображения допускается всего одна повторная попытка. Денежный ledger не используется.
Значения по умолчанию выключают автоматическую попытку; цена задачи известна после ответа API.

Search/detail `Page.goto` дополнительно обёрнуты только на уровне безопасной навигации:
три попытки с stop-aware задержками 1 и 2 секунды для доказанных transient network codes
и Playwright navigation timeout. Исчерпание попыток завершает run с
`STOPPED: hh_network_unavailable`; текущая неоткрытая vacancy не становится manual outcome.
Application/submit этим retry-контуром не охватываются.

## LLM Router и privacy boundary

`llm.generate(task=...)` выбирает enabled connections, фиксирует порядок операции,
нормализует provider failures и выполняет bounded retries/failover. Неизменный Groq request
после HTTP 413 не повторяется на той же connection. Сначала проверяются альтернативы,
затем retries/recovery: operation deadline 120 с, максимум 12 attempts, connect/read timeout
до 5/20 с. Default backoff 2/4/8/10 с; Retry-After не сокращается. После обычных двух rounds
recovery временных 429/5xx/transport failures повторяется до лимитов: ожидание 30 с/operation,
120 с/run. Stop прерывает ожидания и ожидание provider response.
Provider I/O изолирован в daemon worker: caller ждёт результат до общего monotonic deadline,
поздний ответ игнорируется; незавершённый worker блокирует новые операции этого router.
Принудительная отмена уже принятого API-запроса не гарантирована. Caller получает текст или
terminal `LLMUnavailable`/`LLMStopped`. Диагностика содержит UTC start/end, provider attempt
и wait time, attempts, причины исключений/лимита и источник retry delay provider/default;
без prompt/response и credentials.


До router профессиональные источники проходят `sanitize_candidate_text`; persisted records
проходят `sanitize_record`. Provider request не должен включать local credentials кроме
credential самого выбранного provider transport. Raw prompts/responses и секреты не
пишутся в diagnostics. Telegram для employer questions читается только из локального
application profile и никогда не передаётся router. Resume cache не является источником
Telegram; phone/email сохраняют прежний per-resume resolver.

## Application state machine

`application.apply` работает fail-closed: response entry/warnings → scope → questions →
exact resume → letter → validity → network guard → submit → explicit signal. Exact resume
ID обязателен. `before_submit` сначала сохраняет durable `submission_unknown`. Возможный
write без явного успеха остаётся unknown и не повторяется автоматически.

## Diagnostics

Diagnostics — allowlisted structural metadata: stages, reason codes, counts, safe paths,
HTTP status/category, selectors/data-qa names и ограниченные DOM state flags. Не должны
сохраняться DOM text/value, resume/context, contacts, letter, answers, cookies, headers,
tokens или request/response bodies.

Основные семейства: search page, vacancy detail, LLM transport/routing, question evidence,
resume chooser/validation, application request/confirmation и Stop lifecycle.

Для `resume_hidden_warning_requires_action` в standalone response form сохраняются только
структурные признаки warning: display/visibility/opacity, `aria-hidden`, `inert`, геометрия,
пересечение viewport, ограниченная оценка clipping/occlusion, число actionable controls и
отношение найденных exact resume ID к target ID. Перед безопасным отказом выполняется одна
stop-aware стабилизационная проверка длительностью до 400 мс; initial/stabilized snapshots
показывают, сохранился warning или исчез. Hit-test одной точки и clipping являются
диагностическими сигналами, а не доказательством пользовательской видимости.

CSS-visible warning классифицируется как `resume_hidden_warning_requires_action` только
когда после стабилизации в нём остаётся доступный action-control. Без такого control reason
становится `selected_resume_not_verified`; exact-ID guard и запрет submit не меняются.

При неоткрывшемся chooser отдельная bounded-сводка фиксирует тип response state, наличие
active scope, безопасное состояние trigger, popup/option counts до и после клика, transient
popup, наличие exact target ID в scope и время ожидания. Диагностика не содержит DOM text,
значений полей, названий резюме или URL query. Она не меняет outcome: без exact selected
resume ID application остаётся заблокированной до cover letter и submit.

## Test groups

- `test_safe_mvp.py`, `test_current_product_semantics.py` — pipeline и CURRENT contracts;
- `test_live*.py`, `test_dom_application.py`, `test_response_entry.py`,
  `test_warnings_resume.py`, `test_resume_js.py` — LIVE state machine;
- `test_question_*`, `test_optional_questions.py`, `test_evidence_recovery.py` — questions;
- `test_llm_*`, `test_basic_auth_privacy.py` — router/providers/privacy;
- `test_search_*`, `test_vacancy_detail_diagnostics.py` — HH search/detail;
- `test_run_lifecycle.py`, `test_stop_ui.py` — lifecycle/Stop;
- `test_history_*`, `test_current_run_ui.py`, `test_dashboard_history.py` — persistence/UI.

Tests должны быть offline: mock HH/LLM и synthetic candidate data.

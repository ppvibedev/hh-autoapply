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
| `question_sources.py` | локальные resolvers/preference routing |
| `application.py` | response state machine, questions, resume, submit confirmation |
| `history_store.py` | JSONL locking/retention и application safety ledger |
| `webapp.py`, `web/index.html` | API/dashboard/current-run UI/history paging/Stop |

## State и private files

`state/` содержит HH session, resume metadata/cache, decisions journal, application safety,
worker state и control socket. `private/` содержит candidate context, preferences и LLM
connections. Оба каталога runtime-private и игнорируются Git.

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
не попадает в diagnostics. Same-dashboard Stop использует process handle; recovered Stop —
authenticated socket. Correlation выполняется через `stop_attempt_id`.

Доказанная HH CAPTCHA на search/detail navigation переводит worker в общий
`captcha_wait`: stop-aware polling раз в секунду, timeout 120 секунд. После
ручного решения worker повторяет ту же search page или exact vacancy URL;
таймаут завершает run как `STOPPED: hh_captcha_timeout`.

## LLM Router и privacy boundary

`llm.generate(task=...)` выбирает enabled connections, фиксирует порядок операции,
нормализует provider failures и выполняет bounded retries/failover. Caller получает текст
или terminal `LLMUnavailable`/`LLMStopped`.

До router профессиональные источники проходят `sanitize_candidate_text`; persisted records
проходят `sanitize_record`. Provider request не должен включать local credentials кроме
credential самого выбранного provider transport. Raw prompts/responses и секреты не
пишутся в diagnostics.

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

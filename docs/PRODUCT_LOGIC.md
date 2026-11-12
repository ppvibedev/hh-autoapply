# HH Auto Apply — логика продукта

**Status:** CURRENT canonical behavior
**Last verified against commit:** `a1dbd8b2c15098705da3518866cd0417a2a52adc` (production-код до этой documentation migration)

Этот документ описывает наблюдаемое поведение текущей системы. Метки:

- **CURRENT** — реализовано сейчас;
- **PROPOSED** — возможное будущее решение, не реализовано;
- **HISTORICAL** — прежнее поведение или терминология;
- **UNKNOWN** — имеющихся доказательств недостаточно.

## 1. Что делает приложение

**CURRENT.** HH Auto Apply — локальный dashboard и браузерный worker для поиска вакансий
на hh.ru, оценки соответствия выбранному резюме и подготовки отклика. Основной путь
работает через Playwright в сохранённой HH-сессии. LLM используется для matching,
сопроводительного письма и части вопросов работодателя.

## 2. SAFE и LIVE

**CURRENT.** Настройка `RUN_MODE=SAFE` блокирует действия отклика. В SAFE приложение
может искать, читать вакансии, вызывать LLM и готовить письмо, но результат заканчивается
`safe_ready`/`safe_not_sent`.

`RUN_MODE=LIVE` можно сохранить, однако каждый новый LIVE-прогон требует отдельного
подтверждения в dashboard. Только дочерний worker подтверждённого запуска получает
одноразовый признак `HH_LIVE_CONFIRMED_FOR_RUN=1`. Само сохранение LIVE ничего не отправляет.
Неизвестный режим и прямой запуск без одноразового подтверждения безопасно становятся SAFE.

## 3. End-to-end pipeline

**CURRENT.** Последовательность браузерного прогона:

1. Проверка HH-сессии и настроек.
2. Получение exact ID и безопасного title выбранного резюме.
3. Однократное обновление текста выбранного резюме; этот снимок используется весь run.
4. В LIVE — строгая загрузка history и safety ledger.
5. Поиск HH по страницам.
6. Дедупликация exact vacancy ID в текущем run и LIVE-защита от повторной отправки.
7. Hard filters по данным поисковой карточки.
8. Открытие detail page и получение полного описания.
9. LLM matching.
10. Decision order из раздела 10.
11. Для прошедшей вакансии — cover letter.
12. SAFE: запись `safe_ready`. LIVE: вопросы → exact resume verification → письмо →
    проверка формы → submit → интерпретация явного ответа HH.
13. Запись outcome, diagnostics и итогов run.

## 4. Settings

**CURRENT.** Dashboard сохраняет разрешённый набор полей в `.env`: режим, запрос,
регион, зарплату, HH-фильтры, стоп-слова, blacklist, score threshold, лимиты run,
паузы, browser mode, legacy LLM-параметры и выбранное `RESUME_URL`.

Ключ LLM не возвращается клиенту и пустое поле не стирает сохранённый ключ. Несколько
LLM connections хранятся отдельно в приватном локальном файле. Настройки применяются
со следующего run. `DAILY_LIMIT` сейчас служит fallback для
`TARGET_APPLICATIONS_PER_RUN`, а не календарным счётчиком суток.

## 5. Search

**CURRENT.** `SEARCH_TEXT` с несколькими фразами через запятую превращается в запрос
`"фраза 1" OR "фраза 2"`. Пустой `AREA` означает весь мир: параметр `area` не передаётся.
При `SALARY_MIN > 0` передаются `salary=<рубли>` и `currency=RUR`; конвертацию других
валют выполняет HH. Дополнительные непустые HH-фильтры: experience, employment,
schedule, search period и order.

Браузерный run сейчас всегда передаёт `work_format=REMOTE`. Значение `WORK_FORMAT`
сохраняется и показывается, но не меняет этот фактический фильтр.

Нумерация страниц нулевая. `PAGES_PER_RUN` — бюджет страниц, а не доказательство конца
выдачи. Если весь бюджет обработан, итоговая причина сейчас `pages_exhausted`.

## 6. Deduplication

**CURRENT.** Внутри run одна вакансия обрабатывается один раз по exact numeric vacancy ID.
В LIVE persistent safety ledger блокирует повтор для ID со статусом
`applied_confirmed`, `already_applied` или `submission_unknown`. Похожее название не
является ключом. Браузерный SAFE-путь не использует и не обновляет старый `seen.json`.

## 7. Hard filters

**CURRENT.** Hard filters выполняются по поисковой карточке до detail page и LLM:

- blacklist — case-insensitive substring в имени работодателя;
- stopwords — case-insensitive substring только в title вакансии;
- зарплата — только понятная RUB/RUR-верхняя граница ниже `SALARY_MIN`.

Description в stopwords/blacklist не участвует. Элементы списков объединены логикой OR:
достаточно первого совпадения. Морфология, whole-word, дефисы и нормализация `ё/е` не
реализованы. Поэтому `продажи` не обязано совпасть с `продажам`. Иностранная валюта,
отсутствующая зарплата и одна нижняя граница локально не отбрасываются.

## 8. Vacancy detail

**CURRENT.** После hard filters открывается exact vacancy URL, считываются полное
описание и признаки формата работы. Отсутствие canonical description даёт локальный
failure вакансии с privacy-safe structural diagnostics, если строгие CAPTCHA-сигналы
отсутствуют. При доказанной CAPTCHA worker переходит в общий manual pause из раздела 15.

## 9. Matching

**CURRENT.** Matching LLM получает полную вакансию, актуальный sanitized снимок выбранного
резюме, sanitized candidate materials, `INSTRUCTIONS.md`, search intent и безопасный
title выбранного резюме. Контракт требует валидный JSON: capability score 0–100,
decision, matched/missing/risks, remote status, role classification и evidence о роли.
Некорректный или неполный ответ не превращается в догадку и уходит в manual/error.

Score всегда относится к exact resume ID текущего run. Employer questions и response
form в matching не участвуют.

## 10. Exact CURRENT decision order

**CURRENT.** После валидного matching применяется именно такой порядок:

1. `remote_status=ineligible` → `rejected_remote`;
2. score ниже threshold → `rejected_score`;
3. `remote_status=unclear` → `needs_manual_review`;
4. `role_alignment != aligned` → `needs_manual_review`;
5. иначе вакансия допускается к письму/application.

Это означает: доказанная remote-несовместимость отсекается раньше score, но remote
uncertainty проверяется после threshold. Поэтому `remote=unclear` и низкий score дают
`rejected_score`: даже ручное подтверждение remote не позволит вакансии пройти threshold.

## 11. Decision matrix

| Условие после matching | CURRENT outcome | Application |
|---|---|---|
| remote ineligible, любой score | `rejected_remote` | нет |
| remote eligible или unclear, score ниже threshold | `rejected_score` | нет |
| remote unclear, score прошёл | `needs_manual_review` | нет |
| remote eligible, score прошёл, role adjacent/mismatch/unclear | `needs_manual_review` | нет |
| remote eligible, score прошёл, role aligned, SAFE | `safe_ready` | заблокирован |
| то же, LIVE | application flow | только после всех gate |
| matching/privacy/детальная ошибка | `needs_manual_review` или terminal run state | зависит от класса ошибки |

## 12. Manual review

**CURRENT.** `needs_manual_review` объединяет бизнес-неопределённость и технические
fail-closed остановки: неясную удалённость/роль, неподтверждённый ответ, форму или resume,
ошибку DOM и часть ошибок подготовки. Техническая причина уточняется через `reason_code`,
stage и safe diagnostics. Это не означает, что application request был отправлен.

## 13. Privacy и данные для LLM

**CURRENT.** Перед LLM санитизируются выбранное резюме, candidate context,
instructions и данные вакансии. Контакты и технические секреты запрещены; подозрительный
контент fail-closed блокируется. Во внешний LLM всё же уходит sanitized профессиональный
контекст — система не является полностью локальной.

Не отправляются cookies HH, session state, API keys, control tokens и локальные secrets.
Тексты вопросов работодателя и вакансии могут передаваться LLM как рабочие данные.

## 14. LLM providers, retry и failover

**CURRENT.** Поддержаны Anthropic, OpenAI, Yandex, Gemini, Groq и Z.AI; dashboard хранит
несколько локальных connections и позволяет включать/выключать их. Legacy `.env` connection
используется, если отдельная конфигурация отсутствует. Router фиксирует порядок доступных
connections на операцию. Операция ограничена общим wall-clock deadline 120 секунд и
максимум 12 provider attempts. HTTP connect timeout — до 5 секунд, read timeout — до 20
секунд; оба уменьшаются при приближении deadline. Перед ожиданием проверяются доступные
альтернативы. После двух обычных rounds временные 429, 5xx и transport timeout/connection
могут получить дополнительные recovery-проходы в пределах deadline и лимита attempts.
Default backoff растёт 2 → 4 → 8 → 10 секунд для connection; provider Retry-After имеет
приоритет и не сокращается ради раннего повтора. Recovery-ожидания ограничены 30 секундами
на операцию и 120 секундами на run; все ожидания прерываются Stop.
Groq отправляет тот же JSON компактным UTF-8; HTTP 413 исключает повтор идентичного payload
на той же connection в пределах операции. Дневная квота и ошибки доступа исключают
connection до конца run. Они не получают recovery-ожиданий.
При исчерпании deadline, attempts или recovery wait budget run останавливается с
`llm_infrastructure_unavailable`; diagnostics различают конкретный исчерпанный лимит.
Поздний ответ provider после deadline/Stop не используется. Пока такой запрос ещё выполняется,
router не начинает новую операцию. Уже принятый провайдером запрос нельзя гарантированно
отменить; HH submit этим механизмом не повторяется.
Кнопка проверки подключения выполняет только короткий синтетический smoke-test без данных HH
и кандидата. Статус `CONNECTED` подтверждает доступ к API на этом маленьком запросе, но не
означает `READY` для полноразмерных matching, cover-letter или employer-question prompts.

## 15. CAPTCHA CURRENT behavior

**CURRENT.** Строго подтверждённая CAPTCHA на search,
detail или в форме отклика переводит worker в `PAUSED_FOR_CAPTCHA` и удерживает текущую
страницу и вакансию. Для однозначной графической CAPTCHA, если локальная интеграция включена,
ключ задан, worker делает не более двух платных попыток распознавания при смене изображения.
Он вводит ответ только в то же CAPTCHA-окно, пока изображение не изменилось, затем проверяет
состояние HH. При любой неудаче остаётся ручная пауза. Dashboard предлагает решить проверку
в открытом Chromium и нажать «Продолжить после CAPTCHA». После этого worker проверяет, что CAPTCHA отсутствует
и текущая страница имеет ожидаемый для операции путь и структуру. Одно исчезновение маркера
не продолжает run. Если проверка не подтверждена, пауза остаётся. Значение
`CAPTCHA_REMINDER_SECONDS` (по умолчанию 120) создаёт напоминание, но не закрывает браузер
и не завершает run. Stop доступен во время паузы.

При входе в паузу приложение пассивно проверяет видимые структурные признаки известных
провайдеров CAPTCHA во фреймах и графической CAPTCHA в модальном окне (CAPTCHA-маркер,
одно изображение, поле ввода и действие подтверждения). Dashboard показывает `reCAPTCHA`,
`hCaptcha`, `Turnstile`, `текст с картинки` или `unknown`; это определение типа,
а не попытка решить проверку. Без однозначного признака тип остаётся `unknown`.
В журнал попадают только тип, уверенность и ограниченные
счётчики признаков — без URL фреймов, sitekey, изображений, ответов, токенов и текста страницы.
Dashboard позволяет сохранить, заменить или удалить ключ в локальном `.env`, включить или
отключить интеграцию и проверить подключение бесплатным `getBalance`. Ключ не возвращается
в API-ответах. В пределах worker одинаковое изображение не создаёт параллельных или
повторных платных задач. Денежных лимитов нет; цена задачи становится известна после решения.
Строгий CAPTCHA-сигнал проверяется и при раннем выходе из обработки формы: до submit
он ведёт к той же паузе, после возможного submit сохраняет `submission_unknown` без
повторной отправки.

После подтверждения для search повторяется та же страница, для detail — тот же exact
vacancy URL. CAPTCHA до submit позволяет повторить только подготовку формы для той же
вакансии; submit до этого не начинался. CAPTCHA после потенциального submit сохраняет
`submission_unknown` в safety ledger: после ручного продолжения submit не повторяется,
исчезновение CAPTCHA не считается подтверждением отклика. Прерванный worker сохраняет
уже записанный safety ledger; незавершённая пауза остаётся в журнале как событие.

### 15.1. HH navigation resilience

**CURRENT.** Только безопасные переходы на search page и detail page вакансии получают до
трёх попыток. После первой и второй доказанной временной сетевой ошибки ожидание составляет
1 и 2 секунды; ожидание проверяет Stop короткими интервалами. Retry разрешён для
`ERR_INTERNET_DISCONNECTED`, `ERR_NAME_NOT_RESOLVED`, `ERR_NETWORK_IO_SUSPENDED` и
Playwright timeout именно операции `Page.goto`.

Одна восстановившаяся ошибка не создаёт manual outcome. Если все три попытки исчерпаны,
текущая неоткрытая вакансия также не записывается как `needs_manual_review`, а весь run
заканчивается `STOPPED: hh_network_unavailable`. Diagnostics сохраняют operation, число
попыток и безопасную категорию сетевой ошибки без URL/query/session data. Уже записанные
outcomes и safety ledger сохраняются. Application/submit и проверка результата отправки
этими retries не охватываются.

## 16. Resume selection и exact-ID safety

**CURRENT.** Exact HH resume ID текущего run — единственное допустимое доказательство
identity перед LIVE submit. Видимый title и локальный каталог могут использоваться для
display/matching metadata, но не могут преобразовать title обратно в verified ID.

Если response form уже показывает target ID, переключение не требуется. Иначе выбирается
только option с exact ID; после действия ID проверяется повторно. Native/radio/portal пути
разрешены только при exact evidence. Standalone response form без доказанного exact ID
остаётся blocked/manual. Request identity дополнительно проверяется там, где payload
доступен; несовпадение блокирует отправку.

Само наличие CSS-visible `hidden-resume-warning` не доказывает обязательное действие.
Persistent warning с доступным action-control сохраняет
`resume_hidden_warning_requires_action` и никогда не обходится автоматически. Warning без
такого control классифицируется как `selected_resume_not_verified`: submit остаётся
запрещён, потому что exact resume ID всё ещё не подтверждён.

## 17. Employer questions

**CURRENT.** Вопросы — ветка внутри application flow. Простые подтверждённые настройки и
preferences могут разрешаться локально. Явные contact questions не идут в LLM. Telegram
для откликов задаётся в dashboard только в формате `@username`, хранится локально в
`private/application_profile.json` и является единственным источником автоматического
ответа на Telegram-вопрос. Resume cache для этого не используется; оригинальное резюме HH
не изменяется. Телефон и email сохраняют прежнюю логику. Профессиональные открытые вопросы
идут в LLM с question, vacancy, sanitized resume/context и instructions.

Ответ должен иметь подтверждающее evidence. Strict literal validation остаётся финальным
gate; локальный recovery может только найти один однозначный дословный fragment в trusted
candidate sources. Required вопрос без надёжного ответа блокирует application. Optional
вопрос без ответа пропускается. После fill выполняется readback.

## 18. Cover letter

**CURRENT.** Письмо создаётся после всех matching gates: 2–3 коротких абзаца, обычно
600–1000 знаков, 2–3 релевантных подтверждённых факта, без контактов и выдумок. В LIVE
поле открывается/заполняется и проверяется чтением обратно. Ошибка подготовки или проверки
письма блокирует submit.

## 19. LIVE application flow

**CURRENT.** Для вакансии, прошедшей matching:

response entry → известные warnings → response scope → employer questions → exact resume
verification/switch → cover letter → form validation → armed network observer → один
submit action → HH confirmation.

Известные country/language warnings обрабатываются только в предусмотренных состояниях.
Неизвестная форма, обязательное незаполненное поле, неподтверждённое resume или письмо
останавливают вакансию до submit.

## 20. Submission confirmation

**CURRENT.** `applied_confirmed` разрешён только при явном свежем HH success signal.
Явный marker существующего отклика даёт `already_applied`. Один click или наблюдение POST
сами по себе не являются подтверждением результата.

Перед возможной отправкой safety ledger сначала фиксирует `submission_unknown`, чтобы
crash не привёл к слепому повтору.

## 21. `submission_unknown`

**CURRENT.** Если application request мог уйти, но явного подтверждения HH нет, outcome —
`submission_unknown`. Он сохраняется persistent и блокирует автоматический повтор exact
vacancy ID. После возможной отправки приложение не делает blind retry. Проверка результата
в кабинете HH остаётся ручной.

## 22. Stop

**CURRENT.** Dashboard отправляет Stop с correlation ID. Same-dashboard worker сначала
получает запрос через authenticated local control socket; если канал недоступен, остаётся
SIGTERM. Recovered worker также использует authenticated socket. Worker отмечает request,
acknowledgement/observation и заканчивает `STOPPED: user_stop`. После наблюдения Stop новая
vacancy, LLM operation, retry или failover не начинается.

Worker пишет heartbeat примерно каждые 10 секунд, а начало и окончание этапов — отдельно.
Если прогресса нет 180 секунд, он пишет `operation_timeout`, запрашивает кооперативный Stop,
а dashboard показывает зависшее состояние. Уже начатый блокирующий Playwright-вызов
невозможно безопасно прервать из соседнего потока. Stop не обещает мгновенное завершение:
его подтверждает только `run_finished`; при отсутствии подтверждения требуется отдельная
проверка состояния submit перед ручной эскалацией. Если процесс завершился без
`run_finished`, dashboard показывает прерванный незавершённый run вместо состояния «готов».

## 23. History и dashboard

**CURRENT.** `state/decisions.jsonl` — persistent JSONL journal. Он не очищается при старте
run. Retention оставляет пять последних завершённых runs и текущий активный run. Safety
ledger хранится отдельно. Dashboard после перезапуска показывает пустое главное состояние;
он не восстанавливает прошлый run автоматически. После нового запуска экран привязан только
к его run ID. History feed загружается страницами и фильтруется по outcome текущего run.

«Сбросить историю» удаляет журнал, старый `seen.json` и legacy resume cache file, но не
очищает application safety ledger, HH session или per-resume cache directory.

## 24. Canonical outcomes

| Outcome/status | Значение |
|---|---|
| `safe_ready` | SAFE-анализ и письмо готовы, отправки не было |
| `rejected_hard_filter` | сработал локальный hard filter |
| `rejected_remote` | matching подтвердил неподходящую удалённость |
| `rejected_score` | score ниже threshold после gate доказанной remote-несовместимости |
| `needs_manual_review` | решение/форма не подтверждены безопасно |
| `applied_confirmed` | HH явно подтвердил новый отклик |
| `already_applied` | HH явно показал существующий отклик |
| `submission_unknown` | request мог быть отправлен, подтверждения нет; повтор блокируется |
| `apply_failed` | HH явно сообщил о неуспешной отправке либо доказан failure после attempt |

Run status: `COMPLETED`, `STOPPED` или `FAILED`. `pages_exhausted` сейчас означает
исчерпание настроенного page budget. `no_results_or_page_not_recognized` объединяет
0/0, parser failure и некоторые login/CAPTCHA search states и потому не доказывает
нормальный конец выдачи. Исчерпание bounded retries безопасной HH-навигации даёт
`STOPPED: hh_network_unavailable`.

## 25. Known limitations

- Нет доказанного explicit HH empty-results marker; 0/0 остаётся `empty_unclassified`.
- `raw_card_count > 0` и `parsed=0` диагностируется, но run завершается тем же общим reason.
- Search login redirect не имеет отдельного terminal run status.
- `pages_exhausted` — не доказательство конца HH-выдачи.
- Standalone response form может не дать доказанно переключить exact resume ID.
- `needs_manual_review` смешивает бизнес- и технические причины.
- Зависший внутри Playwright вызов может пережить операционный watchdog; он фиксирует
  stall и запрашивает Stop, но не уничтожает процесс во время возможного submit.
- Основной browser path и legacy API path сосуществуют; application через API не считается
  текущим production-маршрутом.

## 26. Open product decisions

Ниже решения **не приняты** и не меняют CURRENT.

| Тема | CURRENT | OPEN QUESTION |
|---|---|---|
| Automatic CAPTCHA solving | явная ручная пауза без автоматического закрытия | После evidence о типе CAPTCHA и допустимом способе решить вопрос внешней integration. |
| `pages_exhausted` | исчерпан configured budget | Переименовать ли, чтобы не звучало как конец HH? |
| Standalone resume form | без exact ID submit запрещён | Какой штатный HH-механизм выбора exact ID? |
| `answer_missing` | manual с имеющейся diagnostic taxonomy | Нужна ли более точная пользовательская причина? |
| `application_flow_error` | общий технический reason | Как разделить доказанные подклассы? |
| Empty search page | 0/0 `empty_unclassified` | Какой stable marker доказывает normal exhaustion? |
| Raw-card parser failure | diagnostic + общий terminal reason | Нужен ли отдельный run status/reason? |
| Manual-review split | один outcome | Разделить business uncertainty и technical failure? |
| Expired HH session | search redirect не даёт отдельный terminal status | Нужен ли fail-fast re-login state? |
| Stop latency | watchdog фиксирует stall; блокирующий Playwright-вызов всё ещё может требовать ручной эскалации | Нужна ли процессная изоляция browser operations? |
| History reset wording | журнал/seen/legacy cache, safety остаётся | Как точнее назвать кнопку и confirmation? |
| Legacy API path | существует отдельно | Поддерживать, изолировать или удалить позже? |

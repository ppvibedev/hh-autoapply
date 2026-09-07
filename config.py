"""Конфиг из .env. Все настройки автопилота в одном месте."""
import os
from dotenv import load_dotenv

# override=True — .env всегда главнее унаследованного окружения.
# Иначе стейл-переменная (напр. DRY_RUN из родителя) молча ломает режим.
load_dotenv(override=True)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "") or ""
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


# hh.ru API
ACCESS_TOKEN = os.getenv("HH_ACCESS_TOKEN", "").strip()
RESUME_ID = os.getenv("HH_RESUME_ID", "").strip()

# Режим: ВСЕГДА боевой. Shadow убран намеренно и жёстко —
# никакое значение в .env/окружении не может включить shadow. Не менять.
DRY_RUN = False

# Предохранители
DAILY_LIMIT = _int("DAILY_LIMIT", 30)
SCORE_THRESHOLD = _int("SCORE_THRESHOLD", 90)

# Фильтры поиска
SEARCH_TEXT = os.getenv("SEARCH_TEXT", "").strip()
# Несколько названий через запятую → ищем по ЛЮБОМУ из них (ИЛИ).
SEARCH_PHRASES = [p.strip() for p in SEARCH_TEXT.split(",") if p.strip()]
# Запрос для hh: при нескольких словосочетаниях — «"фраза1" OR "фраза2" ...».
if len(SEARCH_PHRASES) > 1:
    SEARCH_QUERY = " OR ".join(f'"{p}"' for p in SEARCH_PHRASES)
else:
    SEARCH_QUERY = SEARCH_TEXT
AREA = os.getenv("AREA", "2").strip()
SALARY_MIN = _int("SALARY_MIN", 0)
STOPWORDS = _list("STOPWORDS")
BLACKLIST_EMPLOYERS = _list("BLACKLIST_EMPLOYERS")

# Формат работы: any | remote_ok | remote_only
WORK_FORMAT = os.getenv("WORK_FORMAT", "any").strip()

# Коды hh.ru для параметра work_format по выбранному режиму.
WORK_FORMAT_CODES = {
    "remote_ok": ["REMOTE", "HYBRID"],   # можно удалённо (удалёнка + гибрид)
    "remote_only": ["REMOTE"],           # только полная удалёнка
}.get(WORK_FORMAT, [])                    # any → пусто, без фильтра

# Родные фильтры поиска hh.ru (пусто = не фильтровать). Значения — коды hh.
EXPERIENCE = os.getenv("EXPERIENCE", "").strip()        # noExperience|between1And3|between3And6|moreThan6
EMPLOYMENT = os.getenv("EMPLOYMENT", "").strip()        # full|part|project|probation
SCHEDULE = os.getenv("SCHEDULE", "").strip()            # fullDay|shift|flexible|remote|flyInFlyOut
SEARCH_PERIOD = os.getenv("SEARCH_PERIOD", "").strip()  # 0|1|3|7|30 (дней)
ORDER_BY = os.getenv("ORDER_BY", "publication_time").strip()  # relevance|publication_time|salary_desc|salary_asc

# Собранные родные параметры hh для передачи в поиск (только непустые).
HH_SEARCH_PARAMS = {k: v for k, v in {
    "experience": EXPERIENCE,
    "employment": EMPLOYMENT,
    "schedule": SCHEDULE,
    "search_period": SEARCH_PERIOD,
    "order_by": ORDER_BY,
}.items() if v}

# Файлы состояния
SEEN_FILE = "state/seen.json"          # id вакансий, уже обработанных (дедуп)
DECISIONS_LOG = "state/decisions.jsonl"  # лог всех решений (для калибровки)

# ИИ для сопроводительных писем
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip()  # anthropic | openai | yandex | none
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()  # пусто → дефолт по провайдеру
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()  # нужен для YandexGPT
RESUME_URL = os.getenv("RESUME_URL", "").strip()  # выбранное резюме; пусто = первое
RESUME_FILE = "state/resume.txt"       # кэш текста выбранного резюме
RESUMES_FILE = "state/resumes.json"    # список твоих резюме с hh.ru (для выбора)

# Браузерная автоматизация (Playwright)
SESSION_FILE = "state/session.json"    # сохранённая залогиненная сессия hh.ru
RUN_PID_FILE = "state/run.pid"         # PID активного прогона (точная проверка «идёт ли»)
HEADLESS = os.getenv("HEADLESS", "0") == "1"  # 0 = видимый браузер (нужно для капчи)
PAGES_PER_RUN = _int("PAGES_PER_RUN", 3)      # сколько страниц выдачи разбирать
PAUSE_SECONDS = _int("PAUSE_SECONDS", 6)      # пауза между действиями (антибот)

USER_AGENT = "hh-autoapply/0.1 (personal job search)"

"""Тонкая обёртка над API hh.ru. Только то, что нужно автопилоту."""
import requests
import config

BASE = "https://api.hh.ru"


class HHClient:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": config.USER_AGENT,
        })

    def my_resumes(self) -> list[dict]:
        """GET /resumes/mine — список твоих резюме (id, title)."""
        r = self.s.get(f"{BASE}/resumes/mine", timeout=30)
        r.raise_for_status()
        return r.json().get("items", [])

    def search(self, *, text: str, area: str, salary_min: int,
               work_format_codes: list[str], per_page: int = 50, page: int = 0) -> dict:
        """GET /vacancies — поиск. Возвращает {items, found, pages}."""
        params = {
            "text": text,
            "area": area,
            "per_page": per_page,
            "page": page,
            "order_by": "publication_time",
        }
        if salary_min:
            params["salary"] = salary_min
            params["only_with_salary"] = "true"
        if work_format_codes:
            params["work_format"] = work_format_codes  # список кодов REMOTE/HYBRID
        r = self.s.get(f"{BASE}/vacancies", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def vacancy(self, vacancy_id: str) -> dict:
        """GET /vacancies/{id} — полное описание (нужно для скоринга)."""
        r = self.s.get(f"{BASE}/vacancies/{vacancy_id}", timeout=30)
        r.raise_for_status()
        return r.json()

    def negotiate(self, *, vacancy_id: str, resume_id: str, message: str) -> int:
        """POST /negotiations — РЕАЛЬНЫЙ отклик. Вызывать только вне DRY_RUN.

        Возвращает HTTP-код (201 = успех). Ошибки не глушим — пусть всплывают.
        """
        if config.DRY_RUN:
            raise RuntimeError("safe_mode_submission_disabled")
        # API-flow ещё не поддерживает проверенное matching/резюме нового MVP.
        raise RuntimeError("api_live_not_supported_in_safe_mvp")

"""Чтение и точечное обновление .env (для редактора фильтров в веб-интерфейсе)."""
import os

ENV_PATH = ".env"


def read_env(path: str = ENV_PATH) -> dict[str, str]:
    data: dict[str, str] = {}
    if not os.path.exists(path):
        return data
    with open(path, encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw or raw.strip().startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            # отрезаем inline-комментарий вида  VALUE   # пояснение
            v = v.split("#", 1)[0].strip()
            data[k.strip()] = v
    return data


def update_env(updates: dict[str, str], path: str = ENV_PATH) -> None:
    """Переписывает .env, обновляя значения существующих ключей и добавляя новые.

    Комментарии и порядок строк сохраняются.
    """
    lines: list[str] = []
    seen: set[str] = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                raw = line.rstrip("\n")
                if "=" in raw and not raw.strip().startswith("#"):
                    key = raw.split("=", 1)[0].strip()
                    if key in updates:
                        lines.append(f"{key}={updates[key]}")
                        seen.add(key)
                        continue
                lines.append(raw)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

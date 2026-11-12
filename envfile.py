"""Чтение и точечное обновление .env (для редактора фильтров в веб-интерфейсе)."""
import os
import fcntl
from pathlib import Path
import tempfile

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
    target = Path(path)
    if target.is_symlink():
        raise ValueError("env_symlink_forbidden")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(target) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines: list[str] = []
        seen: set[str] = set()
        if target.exists():
            with target.open(encoding="utf-8") as f:
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
        fd, temp = tempfile.mkstemp(prefix=".env-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write("\n".join(lines) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

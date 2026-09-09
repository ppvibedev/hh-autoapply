"""Только локальные Markdown/TXT из разрешённой папки, без внешних ссылок и symlink."""
from pathlib import Path
from privacy import sanitize_candidate_text

CONTEXT_DIR = Path(__file__).resolve().parent / "private" / "candidate_context"
MAX_CONTEXT_CHARS = 120_000


def load_candidate_context() -> tuple[str, str]:
    root = CONTEXT_DIR
    if not root.exists():
        return "", ""
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError("candidate_context_symlink_not_allowed")
    instructions, documents, total = "", [], 0
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
            raise ValueError("candidate_context_symlink_not_allowed")
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("candidate_context_path_not_allowed")
        with path.open(encoding="utf-8-sig") as stream:
            raw = stream.read(MAX_CONTEXT_CHARS + 1)
        total += len(raw)
        if total > MAX_CONTEXT_CHARS:
            raise ValueError("candidate_context_too_large_120000_chars")
        cleaned = sanitize_candidate_text(raw)
        if relative.as_posix() == "INSTRUCTIONS.md":
            instructions = cleaned
        elif cleaned.strip():
            documents.append(f"Материал {len(documents) + 1}:\n{cleaned}")
    return instructions, "\n\n".join(documents)

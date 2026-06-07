"""Discovery + routing by magic bytes (§3, §16).

For the demo this is just "walk the input path and decide prose vs code"; the
full design adds reconcile/rename/retire/versioning against the DB, which we
skip. Routing prefers magic bytes (real format), falling back to extension.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# extension → language, for code files
CODE_EXTS = {
    ".py": "python",
    ".cs": "csharp",
    ".sh": "bash",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
}

PROSE_EXTS = {".docx", ".doc", ".md", ".markdown", ".txt", ".html", ".htm", ".pdf"}

# Cheap magic-byte signatures → prose route (real format beats extension).
_MAGIC = [
    (b"%PDF-", "prose"),               # pdf
    (b"PK\x03\x04", "prose"),          # docx (zip container)
    (b"\xd0\xcf\x11\xe0", "prose"),    # legacy .doc (OLE2)
]


@dataclass
class Routed:
    path: Path
    route: str                 # "prose" | "code" | "unknown"
    language: str | None = None
    reason: str = ""


def _magic_route(path: Path) -> str | None:
    try:
        head = path.read_bytes()[:8]
    except OSError:
        return None
    for sig, route in _MAGIC:
        if head.startswith(sig):
            return route
    return None


def route_file(path: Path) -> Routed:
    ext = path.suffix.lower()

    if path.stat().st_size == 0:
        return Routed(path, "unknown", reason="empty file → quarantine (§3)")

    magic = _magic_route(path)
    if magic == "prose":
        return Routed(path, "prose", reason="magic bytes")

    if ext in CODE_EXTS:
        return Routed(path, "code", language=CODE_EXTS[ext], reason=f"ext {ext}")
    if ext in PROSE_EXTS:
        return Routed(path, "prose", reason=f"ext {ext}")

    return Routed(path, "unknown", reason=f"unrecognised type {ext or '(none)'}")


def discover(root: Path) -> list[Routed]:
    """Walk `root` (or yield a single file) and route everything found."""
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    return [route_file(p) for p in files]

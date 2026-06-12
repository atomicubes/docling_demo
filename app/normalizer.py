"""Deterministic log normalization.

This module is the canonical contract of the system. The exact same code path
runs at ingestion time and at query time, so identical raw lines always
produce identical signatures and identical hashes.

Rules:
  * Rules are an ORDERED list. Order is load-bearing (timestamps must be
    masked before generic numbers, paths before generic tokens, etc).
  * NORMALIZER_VERSION must be bumped on ANY change to the rules or their
    order. Stored signatures carry the version they were built with; the
    `reindex` CLI command recomputes everything after a bump.
  * No learned state. No online clustering. Pure functions only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Bump on any change to RULES, rule order, or pre/post processing.
NORMALIZER_VERSION = 1

_PATH_SEGMENT = r"[\w.\-@+%~]+"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    replacement: str


def _path_repl(match: re.Match) -> str:
    """Keep only the last two segments of a filesystem path.

    /users/foo/project/config.yaml -> project > config.yaml
    C:\\Users\\foo\\app\\main.py    -> app > main.py
    """
    raw = match.group(0)
    parts = [p for p in re.split(r"[\\/]+", raw) if p]
    tail = parts[-2:] if len(parts) >= 2 else parts
    return " > ".join(tail)


# ---------------------------------------------------------------------------
# ORDERED rule list. Do not reorder without bumping NORMALIZER_VERSION.
# ---------------------------------------------------------------------------
RULES: list[Rule] = [
    # 1. ISO / common timestamps — BEFORE generic numbers.
    # NOTE: normalize() lowercases BEFORE rules run, so all patterns here
    # must match lowercase input (hence IGNORECASE / lowercase literals).
    Rule(
        "timestamp_iso",
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:z|[+-]\d{2}:?\d{2})?\b",
            re.IGNORECASE,
        ),
        "<ts>",
    ),
    Rule(
        "timestamp_syslog",
        re.compile(
            r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b",
            re.IGNORECASE,
        ),
        "<ts>",
    ),
    Rule("time_only", re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<ts>"),
    Rule("date_only", re.compile(r"\b\d{4}[-/]\d{2}[-/]\d{2}\b"), "<date>"),
    # 2. Network identifiers — BEFORE generic numbers.
    Rule(
        "ipv4_port",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b"),
        "<ip>:<num>",
    ),
    Rule("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    Rule(
        "mac",
        re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"),
        "<mac>",
    ),
    Rule(
        "url",
        re.compile(r"\bhttps?://[^\s\"'<>]+", re.IGNORECASE),
        "<url>",
    ),
    Rule(
        "email",
        re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
        "<email>",
    ),
    # 3. Structured IDs — BEFORE generic hex/numbers.
    Rule(
        "uuid",
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    # 4. Filesystem paths — BEFORE generic tokens. Requires >= 2 separators
    #    so plain fractions like 3/4 are untouched.
    Rule(
        "unix_path",
        re.compile(rf"(?:/{_PATH_SEGMENT}){{2,}}/?"),
        "",  # replacement handled via _path_repl in normalize()
    ),
    Rule(
        "windows_path",
        re.compile(rf"\b[A-Za-z]:\\(?:{_PATH_SEGMENT}\\?)+"),
        "",  # replacement handled via _path_repl in normalize()
    ),
    # 5. Quoted payloads — contents are data, not structure.
    Rule("dquoted", re.compile(r'"[^"\n]*"'), "<str>"),
    Rule("squoted", re.compile(r"'[^'\n]*'"), "<str>"),
    # 6. Long hex (hashes, addresses, trace ids) — BEFORE generic numbers.
    Rule("hex_long", re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b"), "<hex>"),
    # 7. Generic numbers — LAST of the value masks.
    Rule("number", re.compile(r"(?<![\w<])\d+(?:\.\d+)?(?![\w>])"), "<num>"),
]

_PATH_RULE_NAMES = {"unix_path", "windows_path"}

# Lines whose severity we keep. Applied to a token like ERROR / [error] / E.
SEVERITY_RE = re.compile(
    r"\b(FATAL|CRITICAL|ERROR|ERR|WARN(?:ING)?|PANIC|SEVERE)\b", re.IGNORECASE
)

# Heuristic for unlevelled logs (print-style output).
ERRORISH_RE = re.compile(
    r"\b(exception|traceback|failed|failure|refused|denied|timeout|timed out"
    r"|cannot|can't|unable|missing|not found|busy|out of memory|oom"
    r"|segfault|stack trace)\b",
    re.IGNORECASE,
)

# Continuation lines that belong to the previous record (stack traces etc).
CONTINUATION_RE = re.compile(
    r"^(\s+at\s|\s+File\s\"|Caused by[:\s]|\s{2,}\S|\t|\.{3}\s*\d+\s+more)"
)


def normalize(line: str) -> str:
    """Map a raw log line to its canonical signature. Pure and deterministic."""
    text = line.strip().lower()
    text = re.sub(r"\s+", " ", text)
    for rule in RULES:
        if rule.name in _PATH_RULE_NAMES:
            text = rule.pattern.sub(_path_repl, text)
        else:
            text = rule.pattern.sub(rule.replacement, text)
    # Collapse whitespace again (masking can join tokens) and trim
    # leading mask debris like "<ts> <ts>" prefixes.
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:<ts> |<date> )+", "", text)
    return text


def signature_hash(signature: str) -> str:
    """SHA-256 of the canonical signature text."""
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def is_error_line(line: str) -> bool:
    """Severity filter: explicit level wins, keyword heuristic as fallback."""
    if SEVERITY_RE.search(line):
        return True
    return bool(ERRORISH_RE.search(line))


def extract_error_records(raw_text: str, max_records: int = 500) -> list[str]:
    """Split raw log text into error *records* (multi-line aware).

    A record is an error/warn line plus any continuation lines that follow it
    (stack frames, 'Caused by', indented context). Only the FIRST line of a
    record is normalized for matching — stack frames are kept on the record
    for display but are too volatile to be part of the signature.
    """
    records: list[str] = []
    current: list[str] | None = None

    for line in raw_text.splitlines():
        if not line.strip():
            if current:
                records.append("\n".join(current))
                current = None
            continue
        if current is not None and CONTINUATION_RE.match(line):
            current.append(line)
            continue
        if current:
            records.append("\n".join(current))
            current = None
        if is_error_line(line):
            current = [line]
        if len(records) >= max_records:
            break

    if current and len(records) < max_records:
        records.append("\n".join(current))
    return records


def record_head(record: str) -> str:
    """First line of a record — the line that carries the signature."""
    return record.split("\n", 1)[0]

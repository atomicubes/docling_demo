"""Secrets redaction — [LIB+GUARD] (§4.3).

The design uses gitleaks rules over the block stream, emitting typed
placeholders like ``<REDACTED:password>`` and flagging chunks. gitleaks is a Go
binary; for a self-contained demo we apply a small set of regex rules in the
same spirit (typed placeholder, per-file counts, `redacted` flag). Swap in real
gitleaks for production.
"""

from __future__ import annotations

import re

from .models import Block, Chunk

# (type, compiled pattern) — ordered, gitleaks-inspired.
_RULES = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("password", re.compile(r"""(?i)\b\w*(?:password|passwd|pwd)\b\s*[:=]\s*['"]?([^\s'"]{4,})""")),
    ("api_key", re.compile(r"""(?i)\b(?:api[_-]?key|secret|token)\s*[:=]\s*['"]?([0-9A-Za-z._\-]{12,})""")),
]


def _redact_text(text: str) -> tuple[str, int]:
    count = 0

    def repl(kind):
        def _r(m):
            nonlocal count
            count += 1
            # keep the assignment lead-in when the rule captured only the value
            if m.groups():
                return m.group(0).replace(m.group(1), f"<REDACTED:{kind}>")
            return f"<REDACTED:{kind}>"
        return _r

    for kind, pat in _RULES:
        text = pat.sub(repl(kind), text)
    return text, count


def redact_blocks(blocks: list[Block]) -> tuple[list[Block], int]:
    """Redact in place over the block stream; returns (blocks, total_count).

    Used on the code path, where blocks are what the code chunker consumes — so
    this is genuinely "redaction before chunking" (§4.3, §16: E → R → chunk).
    """
    total = 0
    for b in blocks:
        new_text, n = _redact_text(b.text)
        if n:
            b.text = new_text
            b.quality_flags.append("redacted")
            total += n
    return blocks, total


def redact_chunks(chunks: list[Chunk]) -> int:
    """Redact prose chunks emitted by the HybridChunker.

    The HybridChunker consumes the DoclingDocument directly, so for the demo we
    enforce the "no secret reaches the index" property on its output instead of
    mutating Docling internals. Returns the total redaction count.
    """
    total = 0
    for c in chunks:
        c.text, n1 = _redact_text(c.text)
        c.text_for_embed, n2 = _redact_text(c.text_for_embed)
        n = max(n1, n2)
        if n:
            c.redactions += n
            if "redacted" not in c.quality_flags:
                c.quality_flags.append("redacted")
            total += n
    return total

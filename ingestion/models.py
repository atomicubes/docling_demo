"""The one internal representation (§2, principle 1).

Everything between extraction and chunking speaks `Block` / `Section`; the
chunker emits `Chunk`. Keeping a single model is what lets Docling (prose) and
tree-sitter/cAST (code) feed the same downstream stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Block:
    """A unit of content from extraction: a heading, paragraph, table row,
    code block, list item, etc. Carries enough structure for the chunker to
    respect section boundaries (§5.1 guards)."""

    kind: str                       # heading | paragraph | table | list_item | code | preamble
    text: str
    section_path: list[str] = field(default_factory=list)  # breadcrumb trail of headings
    level: int | None = None        # heading level (1 = H1) when kind == "heading"
    language: str | None = None     # for code blocks / code files
    start_line: int | None = None   # provenance for code (§4.2)
    end_line: int | None = None
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class Chunk:
    """An index-time-permanent chunk (§2, principle 2)."""

    doc_id: str
    source_path: str
    format: str                     # prose | code
    kind: str                       # prose | code
    section_path: list[str]
    text: str                       # display text
    text_for_embed: str             # breadcrumb-prefixed text actually embedded (§5.2)
    token_count: int
    language: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    redactions: int = 0
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

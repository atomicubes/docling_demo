"""Prose chunking — [LIB+GUARD] Docling HybridChunker (§5.1, §5.2).

HybridChunker is tokenizer-aware and structure-preserving; we configure it with
the §7 tokenizer and the §5.1 budget, then prepend our own breadcrumb and run a
(demo subset of the) guard layer. `repeat_table_header=True` keeps column names
on table-spanning chunks.
"""

from __future__ import annotations

from .models import Chunk
from .tokenizer import MAX_TOKENS, MIN_TOKENS, BREADCRUMB_BUDGET, count_tokens, hf_tokenizer


def _breadcrumb(section_path: list[str]) -> str:
    """Elided section path, middle levels dropped, ≤ BREADCRUMB_BUDGET tokens (§5.2)."""
    if not section_path:
        return ""
    parts = section_path
    if len(parts) > 3:
        parts = [section_path[0], "…", section_path[-2], section_path[-1]]
    crumb = " > ".join(parts)
    # trim from the front (keep the most specific levels) until within budget
    while count_tokens(crumb) > BREADCRUMB_BUDGET and " > " in crumb:
        crumb = crumb.split(" > ", 1)[1]
    return crumb


def _guard(chunk: Chunk) -> None:
    """Demo subset of the §5.1 guard layer. Real design falls back to a
    recursive splitter on violation; here we flag and count."""
    if chunk.token_count > MAX_TOKENS:
        chunk.quality_flags.append("over_max_tokens")
    if chunk.token_count < MIN_TOKENS:
        # HybridChunker's documented undersized-tail behaviour (§5.1)
        chunk.quality_flags.append("undersized")
    if not chunk.section_path:
        chunk.quality_flags.append("no_section")


def chunk_prose(doc, doc_id: str, source_path: str) -> list[Chunk]:
    from docling.chunking import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

    tokenizer = HuggingFaceTokenizer(tokenizer=hf_tokenizer(), max_tokens=MAX_TOKENS)
    chunker = HybridChunker(tokenizer=tokenizer, merge_peers=True)

    chunks: list[Chunk] = []
    for dl_chunk in chunker.chunk(dl_doc=doc):
        section_path = list(getattr(dl_chunk.meta, "headings", None) or [])
        text = dl_chunk.text
        crumb = _breadcrumb(section_path)
        text_for_embed = f"[{crumb}]\n{text}" if crumb else text

        chunk = Chunk(
            doc_id=doc_id,
            source_path=source_path,
            format="prose",
            kind="prose",
            section_path=section_path,
            text=text,
            text_for_embed=text_for_embed,
            token_count=count_tokens(text_for_embed),
        )
        _guard(chunk)
        chunks.append(chunk)

    return chunks

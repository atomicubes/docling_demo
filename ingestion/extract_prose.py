"""Prose extraction — [LIB] Docling (§4.1).

We skip the LibreOffice pre-pass (§3 / §4.1) for the demo, so legacy `.doc` and
tracked-changes flattening are out; Docling handles docx/md/txt/html/pdf
directly. The DoclingDocument is normalised into our Block/Section model — the
seam that keeps Docling swappable (§4.1, last bullet).

The DoclingDocument is also handed straight to the HybridChunker (§5.1), so the
block list we build here is used for the unified model + redaction; chunking
itself consumes the DoclingDocument.
"""

from __future__ import annotations

from pathlib import Path

from .models import Block


def extract_prose(path: Path):
    """Return (docling_document, [Block]). The blocks are the redaction +
    audit view; the DoclingDocument feeds the HybridChunker."""
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(str(path))
    doc = result.document

    blocks: list[Block] = []
    section_path: list[str] = []

    # Walk items in reading order, tracking the heading hierarchy so each block
    # carries its breadcrumb trail (§5.2).
    for item, _level in doc.iterate_items():
        label = getattr(item, "label", None)
        label = str(label) if label is not None else ""
        text = (getattr(item, "text", "") or "").strip()

        if "header" in label or "title" in label or "section_header" in label:
            if not text:
                continue
            lvl = getattr(item, "level", None) or 1
            # maintain a simple heading stack
            section_path = section_path[: max(lvl - 1, 0)]
            section_path = section_path + [text]
            blocks.append(Block(kind="heading", text=text, level=lvl,
                                section_path=list(section_path)))
        elif "code" in label:
            if text:
                blocks.append(Block(kind="code", text=text,
                                    section_path=list(section_path)))
        elif "table" in label:
            # serialize row-wise (§4.1); fall back to whatever text is present
            try:
                md = item.export_to_markdown(doc)
            except Exception:
                md = text
            if md.strip():
                blocks.append(Block(kind="table", text=md.strip(),
                                    section_path=list(section_path)))
        elif "list" in label:
            if text:
                blocks.append(Block(kind="list_item", text=text,
                                    section_path=list(section_path)))
        else:
            if text:
                blocks.append(Block(kind="paragraph", text=text,
                                    section_path=list(section_path)))

    return doc, blocks

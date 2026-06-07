#!/usr/bin/env python3
"""Demo ingestion CLI — the v5 pipeline up to chunking (§2, §16).

    discover & route → extract (Docling | tree-sitter+cAST)
        → unified block/section model → secrets redaction → chunking + guards
            → write chunks to an output file

The LibreOffice pre-pass (§3), embedding, storage, and the whole retrieval side
are out of scope. Run:

    uv run ingest.py <input-path> [--out output/chunks.jsonl]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from ingestion.discover import discover
from ingestion.extract_prose import extract_prose
from ingestion.chunk_prose import chunk_prose
from ingestion.chunk_code import chunk_code
from ingestion.redact import redact_chunks


def _doc_id(path: Path) -> str:
    h = hashlib.sha1()
    h.update(path.read_bytes())
    return h.hexdigest()[:12]


def ingest_file(routed) -> tuple[list, list[str]]:
    """Returns (chunks, notes). Notes capture quarantine reasons / errors."""
    path = routed.path
    if routed.route == "unknown":
        return [], [f"QUARANTINE {path}: {routed.reason}"]

    doc_id = _doc_id(path)
    try:
        if routed.route == "prose":
            doc, _blocks = extract_prose(path)            # §4.1
            chunks = chunk_prose(doc, doc_id, str(path))  # §5.1 + guards
        else:  # code
            chunks = chunk_code(path, routed.language, doc_id)  # §4.2 cAST
    except Exception as e:  # noqa: BLE001 — demo: quarantine on any failure (§3)
        return [], [f"QUARANTINE {path}: {type(e).__name__}: {e}"]

    n_redacted = redact_chunks(chunks)                    # §4.3
    note = f"OK       {path}  [{routed.route}]  {len(chunks)} chunks"
    if n_redacted:
        note += f", {n_redacted} secrets redacted"
    return chunks, [note]


def main() -> int:
    ap = argparse.ArgumentParser(description="v5 ingestion demo (up to chunking)")
    ap.add_argument("input", type=Path, help="file or directory to ingest")
    ap.add_argument("--out", type=Path, default=Path("output/chunks.jsonl"))
    args = ap.parse_args()

    if not args.input.exists():
        print(f"error: {args.input} not found", file=sys.stderr)
        return 1

    routed = discover(args.input)
    print(f"discovered {len(routed)} file(s)\n")

    all_chunks, notes = [], []
    for r in routed:
        chunks, file_notes = ingest_file(r)
        all_chunks.extend(chunks)
        notes.extend(file_notes)

    for n in notes:
        print(n)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        for c in all_chunks:
            f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")

    # summary
    by_fmt = Counter(c.format for c in all_chunks)
    flags = Counter(flag for c in all_chunks for flag in c.quality_flags)
    print("\n" + "─" * 56)
    print(f"wrote {len(all_chunks)} chunks → {args.out}")
    print(f"  by format: {dict(by_fmt)}")
    if flags:
        print(f"  guard/quality flags: {dict(flags)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

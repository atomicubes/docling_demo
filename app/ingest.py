"""Ingestion: curated (symptom logs -> POA) pairs into the store.

Ingestion runs the SAME normalizer as the query path — that symmetry is the
whole system. A POA is stored once; every error line in its symptom logs
becomes one signature row pointing at it.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db
from .normalizer import (
    extract_error_records,
    normalize,
    record_head,
    signature_hash,
)


@dataclass
class IngestReport:
    poa_id: int
    signatures_added: int
    signatures_skipped: int  # duplicates of existing (hash, poa) pairs
    lines_ignored: int       # non-error lines in the symptom logs


def ingest_incident(title: str, steps_md: str, symptom_logs: str) -> IngestReport:
    """Store one curated incident: a POA plus signatures from its logs."""
    records = extract_error_records(symptom_logs)
    if not records:
        raise ValueError(
            "No error/warn records found in symptom logs; nothing to index."
        )

    poa_id = db.insert_poa(title, steps_md)
    added = skipped = 0
    seen_hashes: set[str] = set()

    for rec in records:
        head = record_head(rec)
        sig = normalize(head)
        if not sig:
            continue
        h = signature_hash(sig)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        if db.insert_signature(h, sig, head.strip(), poa_id) is not None:
            added += 1
        else:
            skipped += 1

    total_lines = len(symptom_logs.splitlines())
    return IngestReport(
        poa_id=poa_id,
        signatures_added=added,
        signatures_skipped=skipped,
        lines_ignored=max(total_lines - len(records), 0),
    )


def reindex_all() -> int:
    """Recompute sig_text/sig_hash for every stored signature.

    Run after bumping NORMALIZER_VERSION. Idempotent. Returns rows updated.
    """
    updated = 0
    for row in db.all_signatures_for_reindex():
        sig = normalize(row["raw_example"])
        db.update_signature_canonical(row["id"], signature_hash(sig), sig)
        updated += 1
    return updated

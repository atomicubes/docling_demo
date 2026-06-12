"""Matching engine.

Pipeline per analyze request:
  1. extract error records (multi-line aware) from raw text
  2. normalize each record head -> signature text + hash
  3. batch exact-hash lookup (precision leg)
  4. trigram fuzzy fallback for misses (drift leg)
  5. aggregate votes per POA, weighted by match quality
  6. decision gate: confident match / candidates / abstain

The vote aggregation is pure Python (no DB) so it is unit-testable; only the
two lookup functions are injected.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from . import db
from .config import settings
from .normalizer import (
    extract_error_records,
    normalize,
    record_head,
    signature_hash,
)

EXACT_WEIGHT = 1.0  # an exact hash hit is a full-confidence vote


@dataclass
class Evidence:
    """One matched (or unmatched) error record from the user's logs."""

    user_line: str
    signature: str
    match_type: str  # "exact" | "fuzzy" | "none"
    matched_example: str | None = None
    matched_signature: str | None = None
    similarity: float | None = None
    poa_id: int | None = None
    full_record: str = ""


@dataclass
class PoaCandidate:
    poa_id: int
    title: str
    score: float
    exact_hits: int
    fuzzy_hits: int
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class MatchResult:
    verdict: str  # "match" | "candidates" | "no_match" | "no_errors"
    confidence: float
    candidates: list[PoaCandidate]
    unmatched: list[Evidence]
    records_analyzed: int


def _dedupe_records(records: list[str]) -> list[tuple[str, str, str, int]]:
    """(record, signature, hash, occurrence_count), deduped by signature.

    The same root cause often emits the same line hundreds of times; one
    signature should be one vote regardless of repetition, but we keep the
    count for display.
    """
    seen: dict[str, list] = {}
    order: list[str] = []
    for rec in records:
        sig = normalize(record_head(rec))
        if not sig:
            continue
        if sig in seen:
            seen[sig][3] += 1
        else:
            seen[sig] = [rec, sig, signature_hash(sig), 1]
            order.append(sig)
    return [tuple(seen[s]) for s in order]  # type: ignore[return-value]


def match_records(
    records: list[str],
    *,
    exact_lookup: Callable[[list[str]], dict] = db.lookup_exact,
    fuzzy_lookup: Callable[[str, float], dict | None] = db.lookup_fuzzy,
    fuzzy_threshold: float | None = None,
    abstain_threshold: float | None = None,
) -> MatchResult:
    fuzzy_threshold = (
        settings.fuzzy_threshold if fuzzy_threshold is None else fuzzy_threshold
    )
    abstain_threshold = (
        settings.abstain_threshold
        if abstain_threshold is None
        else abstain_threshold
    )

    deduped = _dedupe_records(records)
    if not deduped:
        return MatchResult("no_errors", 0.0, [], [], 0)

    hashes = [h for _, _, h, _ in deduped]
    exact = exact_lookup(hashes)

    evidence: list[Evidence] = []
    titles: dict[int, str] = {}
    for rec, sig, h, _count in deduped:
        head = record_head(rec)
        hit = exact.get(h)
        if hit:
            evidence.append(
                Evidence(
                    user_line=head,
                    signature=sig,
                    match_type="exact",
                    matched_example=hit["raw_example"],
                    matched_signature=hit["sig_text"],
                    similarity=1.0,
                    poa_id=hit["poa_id"],
                    full_record=rec,
                )
            )
            continue
        fz = fuzzy_lookup(sig, fuzzy_threshold)
        if fz:
            titles[fz["poa_id"]] = fz["poa_title"]
            evidence.append(
                Evidence(
                    user_line=head,
                    signature=sig,
                    match_type="fuzzy",
                    matched_example=fz["raw_example"],
                    matched_signature=fz["sig_text"],
                    similarity=fz["similarity"],
                    poa_id=fz["poa_id"],
                    full_record=rec,
                )
            )
        else:
            evidence.append(
                Evidence(
                    user_line=head,
                    signature=sig,
                    match_type="none",
                    full_record=rec,
                )
            )

    # ---- aggregate votes per POA -----------------------------------------
    by_poa: dict[int, PoaCandidate] = {}
    for h, hit in exact.items():
        titles[hit["poa_id"]] = hit["poa_title"]
    for ev in evidence:
        if ev.poa_id is None:
            continue
        cand = by_poa.get(ev.poa_id)
        if cand is None:
            cand = PoaCandidate(
                poa_id=ev.poa_id,
                title=titles.get(ev.poa_id, ""),
                score=0.0,
                exact_hits=0,
                fuzzy_hits=0,
            )
            by_poa[ev.poa_id] = cand
        weight = EXACT_WEIGHT if ev.match_type == "exact" else (ev.similarity or 0.0)
        cand.score += weight
        if ev.match_type == "exact":
            cand.exact_hits += 1
        else:
            cand.fuzzy_hits += 1
        cand.evidence.append(ev)

    # fill titles for fuzzy-only candidates
    for cand in by_poa.values():
        if not cand.title:
            poa = db.get_poa(cand.poa_id)
            cand.title = poa["title"] if poa else f"POA #{cand.poa_id}"

    candidates = sorted(by_poa.values(), key=lambda c: c.score, reverse=True)
    unmatched = [e for e in evidence if e.match_type == "none"]

    if not candidates:
        return MatchResult("no_match", 0.0, [], unmatched, len(deduped))

    top = candidates[0]
    # Confidence: top score normalized by matched signature count, scaled by
    # the margin over the runner-up. Simple, monotone, explainable.
    matched_n = top.exact_hits + top.fuzzy_hits
    base = top.score / matched_n if matched_n else 0.0
    if len(candidates) > 1 and top.score > 0:
        margin = (top.score - candidates[1].score) / top.score
        confidence = base * (0.5 + 0.5 * margin)
    else:
        confidence = base

    if confidence >= abstain_threshold and (
        len(candidates) == 1 or top.score > candidates[1].score
    ):
        verdict = "match"
    elif candidates:
        verdict = "candidates"
        confidence = min(confidence, abstain_threshold)
    else:  # pragma: no cover - guarded above
        verdict = "no_match"

    return MatchResult(verdict, round(confidence, 3), candidates, unmatched, len(deduped))


def analyze_text(raw_text: str) -> MatchResult:
    """Full pipeline entry point used by the API."""
    records = extract_error_records(raw_text)
    result = match_records(records)
    # Flywheel: persist what we couldn't match for later curation.
    for ev in result.unmatched:
        try:
            db.record_unmatched(ev.signature, ev.full_record)
        except Exception:  # noqa: BLE001 - flywheel write must never fail a query
            pass
    return result

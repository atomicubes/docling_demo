from app.matcher import match_records
from app.normalizer import normalize, signature_hash


def _store(*pairs):
    """Build fake exact/fuzzy lookups from (raw_line, poa_id, title) tuples."""
    by_hash = {}
    rows = []
    for raw, poa_id, title in pairs:
        sig = normalize(raw)
        row = {
            "sig_text": sig,
            "raw_example": raw,
            "poa_id": poa_id,
            "poa_title": title,
        }
        by_hash[signature_hash(sig)] = row
        rows.append(row)

    def exact(hashes):
        return {h: by_hash[h] for h in hashes if h in by_hash}

    def fuzzy(sig_text, threshold):
        # crude token-overlap stand-in for pg_trgm, good enough for tests
        best, best_sim = None, 0.0
        q = set(sig_text.split())
        for row in rows:
            t = set(row["sig_text"].split())
            sim = len(q & t) / max(len(q | t), 1)
            if sim > best_sim:
                best, best_sim = row, sim
        if best and best_sim > threshold:
            return {**best, "similarity": best_sim}
        return None

    return exact, fuzzy


SEED = [
    ("ERROR server failed to start: port 4000 busy!", 1, "Port in use"),
    ("ERROR config missing at /users/foo/project/config.yaml", 2, "Missing config"),
]


def test_exact_match_high_confidence():
    exact, fuzzy = _store(*SEED)
    res = match_records(
        ["ERROR server failed to start: port 9999 busy!"],
        exact_lookup=exact,
        fuzzy_lookup=fuzzy,
        abstain_threshold=0.45,
    )
    assert res.verdict == "match"
    assert res.candidates[0].poa_id == 1
    assert res.candidates[0].exact_hits == 1
    assert res.confidence == 1.0


def test_fuzzy_fallback_on_drifted_wording():
    exact, fuzzy = _store(*SEED)
    res = match_records(
        ["ERROR failed to start server: port 8080 busy!"],  # reordered words
        exact_lookup=exact,
        fuzzy_lookup=fuzzy,
        fuzzy_threshold=0.5,
    )
    assert res.verdict in ("match", "candidates")
    assert res.candidates[0].poa_id == 1
    assert res.candidates[0].fuzzy_hits == 1


def test_vote_aggregation_picks_majority_poa():
    exact, fuzzy = _store(
        *SEED,
        ("ERROR bind: address already in use 0.0.0.0:4000", 1, "Port in use"),
    )
    res = match_records(
        [
            "ERROR server failed to start: port 4000 busy!",
            "ERROR bind: address already in use 0.0.0.0:4000",
            "ERROR config missing at /users/bar/other/config.yaml",
        ],
        exact_lookup=exact,
        fuzzy_lookup=fuzzy,
    )
    assert res.candidates[0].poa_id == 1
    assert res.candidates[0].score > res.candidates[1].score


def test_repeated_lines_count_once():
    exact, fuzzy = _store(*SEED)
    res = match_records(
        ["ERROR server failed to start: port 4000 busy!"] * 50,
        exact_lookup=exact,
        fuzzy_lookup=fuzzy,
    )
    assert res.records_analyzed == 1
    assert res.candidates[0].score == 1.0


def test_abstain_on_unknown_error():
    exact, fuzzy = _store(*SEED)
    res = match_records(
        ["ERROR kernel panic: synergy overflow in flux capacitor"],
        exact_lookup=exact,
        fuzzy_lookup=fuzzy,
        fuzzy_threshold=0.6,
    )
    assert res.verdict == "no_match"
    assert res.unmatched and res.candidates == []


def test_no_errors_verdict():
    exact, fuzzy = _store(*SEED)
    res = match_records([], exact_lookup=exact, fuzzy_lookup=fuzzy)
    assert res.verdict == "no_errors"

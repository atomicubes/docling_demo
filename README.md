# log2poa

Raw logs in → Plan of Action out.

A signature-matching incident-response tool: curated incidents (symptom logs +
their remediation POA) are indexed as deterministic log signatures; new logs
from users are normalized through the *same* pipeline and matched against the
knowledge base — exact hash first, trigram similarity as fallback — with
file-level vote aggregation and an explicit abstain path.

## Architecture in one paragraph

`normalizer.py` is the contract: a versioned, ordered list of masking rules
(timestamps → network ids → uuids → paths → quotes → hex → numbers) that maps
any raw line to a canonical signature, deterministically. Ingestion and query
run the identical function, so identical errors always hash identically.
Matching (`matcher.py`) is three tiers: exact SHA-256 hash hit (precision),
pg_trgm similarity above a threshold (drift tolerance), and abstain (honesty).
Multiple error records in one file vote for POAs; the winner needs both a
confidence above threshold and a margin over the runner-up, otherwise the
system returns ranked candidates or "unknown error". Everything unmatched is
queued in `unmatched_query` — the curation flywheel.

## Run it

```bash
docker compose up -d --build
# seed the knowledge base
docker compose exec api python cli.py seed seed/seed_example.yaml
```

Or locally: start Postgres, `pip install -r requirements.txt`, then
`uvicorn app.main:app` and `python cli.py seed seed/seed_example.yaml`.

## Demo script

```bash
# 1. Exact match — same error class, different port (values are masked)
curl -s localhost:8000/analyze -H 'Content-Type: application/json' -d '{
  "logs": "2026-06-12T14:01:03Z ERROR server failed to start: port 5050 busy!"
}' | python3 -m json.tool

# 2. Fuzzy match — wording drifted, trigram catches it, similarity reported
curl -s localhost:8000/analyze -H 'Content-Type: application/json' -d '{
  "logs": "ERROR failed to start the server because port 7777 is busy"
}' | python3 -m json.tool

# 3. Unknown error — system says so, queues it for curation
curl -s localhost:8000/analyze -H 'Content-Type: application/json' -d '{
  "logs": "ERROR quantum desync in module hyperdrive"
}' | python3 -m json.tool
curl -s localhost:8000/unmatched | python3 -m json.tool

# 4. Whole file upload
curl -s -F "file=@/var/log/myapp.log" localhost:8000/analyze/file
```

The response always includes an evidence table — *your* log line next to the
documented line it matched, with match type and similarity. That side-by-side
is the credibility of the demo.

## Adding incidents

```bash
curl -s -X POST localhost:8000/incidents -H 'Content-Type: application/json' -d '{
  "title": "Disk full on /var",
  "steps_md": "1. `df -h /var` ...\n2. rotate logs ...",
  "symptom_logs": "2026-06-12T10:00:00Z ERROR write failed: no space left on device"
}'
```

Or batch-load from YAML: `python cli.py seed seed/seed_example.yaml`.

## Operational notes

- **Normalizer changes**: any edit to rules or their order requires bumping
  `NORMALIZER_VERSION` in `app/normalizer.py`, then `python cli.py reindex`
  to recompute all stored signatures from their `raw_example`. Cheap at this
  scale, atomic enough for a single instance.
- **Thresholds**: `FUZZY_THRESHOLD` (default 0.60) and `ABSTAIN_THRESHOLD`
  (default 0.45) are env vars. Tune them against a held-out set before
  trusting them — never by feel.
- **LLM presentation**: set `ANTHROPIC_API_KEY` to have Claude rewrite the
  POA grounded in the user's actual values (real port, real path). The stored
  POA is the source of truth; the model is instructed to adapt, never invent
  steps, and any failure falls back to deterministic rendering.
- **Debugging a signature**: `python cli.py normalize "raw line here"` prints
  the canonical form and hash.

## Tests

```bash
python -m pytest tests/   # 15 tests, no database required
```

The normalizer and vote-aggregation logic are pure functions tested in
isolation; DB lookups are injected, so the matcher tests run with fakes.

## Where this goes after the POC

The architecture has deliberate seams for the production version:
1. **Semantic leg**: add a pgvector column with embeddings of LLM-generated
   symptom summaries as a third retrieval leg, fused with the existing two.
2. **Rarity weighting**: weight signature votes by inverse corpus frequency
   so chronic noise lines stop diluting confidence.
3. **Disambiguation**: when verdict is `candidates`, derive one targeted
   question from the discriminating signature instead of just ranking.
4. **Eval harness**: golden set of logs→POA pairs plus known-negative cases;
   gate threshold and rule changes on precision@1 and abstain accuracy.

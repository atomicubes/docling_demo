# Log → Signature Ingestion (Single-line)

Context spec for the coding agent. Scope: **single-line logs only**. Multi-line stack traces are out of scope for now (handled in a later phase).

## Goal

Convert a parsed single-line log into a stable **signature** that is:
- the **dedup key** (`WHERE signature_hash = $1`), and
- the **semantic search target** (embedding is generated from the signature, not the raw line).

A signature is the invariant skeleton of a log message: static template with variable parts replaced by typed placeholders. Two logs with the same skeleton = the same kind of event.

## Decision: regex masking, NOT Drain3 (for now)

Use deterministic regex masking. Rationale:

- **Hash stability.** The dedup gate requires the same log → the same hash, for the system's lifetime. Regex is **stateless**: a template depends only on the log, never on ingest history. Drain3 is **stateful** — the assigned template depends on accumulated tree state, so the same log hashes differently early vs. late. Never hash a Drain3 template.
- **Single-example testability.** Regex works correctly from log #1. Drain3 needs volume to generalize, which we don't have during build/test.
- **Air-gapped simplicity.** Regex has no mutable state to snapshot/recover/version.

Trade-off accepted: regex under-generalizes on **untyped identifiers** (hostnames, service names, queue names) that match no value-type rule. This is tracked (see Instrumentation) and is the future trigger for a Drain3 batch pass. It is NOT a blocker now.

Migration is non-destructive: `raw_message` is persisted on every event, so the accumulated corpus becomes the Drain3 training set later if/when data justifies it.

## Pipeline

Input: parser has already extracted `message`, `timestamp`, `service`, `severity`, and other structured fields. We operate on `message` only.

```
message → ordered mask → template_text → sha256 → signature_hash
```

1. **Ordered mask** — apply regex substitutions most-specific → least-specific. Order is load-bearing: a UUID contains hex, an IP contains digits, an epoch is "just a number." Wrong order destroys the specific type before it can be matched.
2. **template_text** — the masked, canonical string. Deterministic.
3. **signature_hash** — `sha256(template_text.encode())`, BYTEA. The dedup key.

### Mask order (apply in this sequence)

| # | Pattern | Placeholder |
|---|---------|-------------|
| 1 | UUID/GUID `[0-9a-fA-F]{8}-…-[0-9a-fA-F]{12}` | `<GUID>` |
| 2 | IPv4 `\b\d{1,3}(\.\d{1,3}){3}\b` | `<IP>` |
| 3 | Hex `0x[0-9a-fA-F]+` | `<HEX>` |
| 4 | Epoch-ish `\b\d{10,13}\b` | `<TS>` |
| 5 | Path `/[\w./-]+` | `<PATH>` |
| 6 | Bare number `\b\d+\b` | `<NUM>` |

**Do NOT mask:** exception class names, error codes, status codes, enum-like keywords. These carry meaning and must stay in the template (and thus the vector).

**Do NOT inject** service/severity into `template_text`. Service is a structured column and a filter/boost dimension at query time — baking it in would split signatures that are shared across services.

```python
import re
from hashlib import sha256

MASKS = [
    (re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'), '<GUID>'),
    (re.compile(r'\b\d{1,3}(\.\d{1,3}){3}\b'), '<IP>'),
    (re.compile(r'0x[0-9a-fA-F]+'), '<HEX>'),
    (re.compile(r'\b\d{10,13}\b'), '<TS>'),
    (re.compile(r'/[\w./-]+'), '<PATH>'),
    (re.compile(r'\b\d+\b'), '<NUM>'),
]

def canonicalize(message: str) -> str:
    out = message
    for pat, repl in MASKS:
        out = pat.sub(repl, out)
    return re.sub(r'\s+', ' ', out).strip()

def signature_hash(template_text: str) -> bytes:
    return sha256(template_text.encode()).digest()
```

## Hash vs. embedding

- **Hash** ← `template_text` (lean, deterministic). Owns identity/dedup.
- **Embedding** ← `template_text` for now. (Later phases may embed a richer rendering; the vector tolerates non-determinism, the hash does not. Keep an `embed_source` column to record which.)

Only embed **novel** signatures. The dedup gate runs before the embedder, so repeated logs cost a hash lookup, not an inference.

## Ingestion path (per event)

```python
template = canonicalize(message)
sig_hash = signature_hash(template)

row = db.fetchrow("SELECT id FROM log_signature WHERE signature_hash=$1", sig_hash)
if row is None:                       # novel → embed once, insert
    vec = embed(template)
    row = db.fetchrow("""
        INSERT INTO log_signature (signature_hash, template_text, embedding, embedding_model)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (signature_hash) DO NOTHING
        RETURNING id
    """, sig_hash, template, vec, MODEL)
    if row is None:                   # lost race → re-read
        row = db.fetchrow("SELECT id FROM log_signature WHERE signature_hash=$1", sig_hash)

signature_id = row["id"]

db.execute("""INSERT INTO log_event
    (signature_id, service, severity, event_timestamp, raw_message, structured_fields, file_ref_id)
    VALUES ($1,$2,$3,$4,$5,$6,$7)""",
    signature_id, service, severity, ts, raw_message, fields, file_ref_id)

db.execute("""INSERT INTO signature_service (signature_id, service, occurrence_count, first_seen, last_seen)
    VALUES ($1,$2,1,$3,$3)
    ON CONFLICT (signature_id, service) DO UPDATE
       SET occurrence_count = signature_service.occurrence_count + 1,
           last_seen = EXCLUDED.last_seen""",
    signature_id, service, ts)
```

- `ON CONFLICT DO NOTHING` on the unique `signature_hash` makes concurrent ingesters safe; the loser re-reads.
- `raw_message` is always preserved (audit + future Drain3 training).
- Cross-service correlation needs no extra logic: the signature is service-agnostic, so a second service emitting a matching log just adds a `signature_service` row. One signature, one embedding, N services.

## Schema touchpoints

Relevant tables (PostgreSQL + pgvector):

- `log_signature(id, signature_hash BYTEA UNIQUE, template_text, embedding VECTOR(n), embedding_model, embed_source)`
- `log_event(id, signature_id FK, service, severity, event_timestamp, raw_message, structured_fields JSONB, file_ref_id FK)`
- `signature_service(signature_id FK, service, occurrence_count, first_seen, last_seen, PK(signature_id, service))`

Unique constraint on `log_signature.signature_hash` is the dedup anchor. HNSW index on `embedding` (`vector_cosine_ops`).

## Instrumentation (ship from day one)

Cheap diagnostic to make the future regex→Drain3 decision data-driven, not a guess:

1. **Signatures created per 1,000 logs** — track over time.
2. **Near-duplicate template scan** — periodically find signature pairs whose `template_text` differs in exactly one token position. A growing cluster of these is the fingerprint of regex under-masking an untyped identifier.

While these stay flat, regex rigidity costs nothing — do not add Drain3. When near-dup clusters climb, that's the empirical trigger to batch-run Drain3 over the accumulated `raw_message` corpus and migrate signatures.

## Out of scope (later phases)

- Multi-line / stack-trace signatures (different extraction: exception type + normalized frame sequence, or message-based when the frame is shared plumbing).
- Drain3 adaptive templating (gated on the instrumentation trigger above).
- Reranker / LLM query rewrite at retrieval time.

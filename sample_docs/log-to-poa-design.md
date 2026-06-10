# Log → Plan-of-Action: Ingestion & Matching Design (Case-Based Reasoning)

**What this system is:** a *solved-incident knowledge base*. The ingested unit is an **Incident Case** = a canonical log fingerprint paired with a Plan of Action (POA). At query time a user drops in a (filtered) log; the system canonicalizes it to a fingerprint, matches it against ingested cases, and returns the associated POA(s) **with provenance and a confidence stance — or an explicit "no known case."**

**How it relates to the v5 guide system:** this is a **sibling index, not a fork.** It reuses the same Postgres instance, the same Drain3 masking config (the shared canonicalization contract is the whole reason both can coexist), the same embedding model, and the same eval/lifecycle discipline. It adds one new table family and one new retrieval flow. A POA can *cross-link* to guide sections in the v5 index — "do this, and here's the doc that explains why." Don't build a parallel stack.

**Assumptions this plan is built on (challenge these):**
1. Cases are **human-curated, appended at resolution time.** (If auto-harvested from tickets → see §9.)
2. A case is keyed on a **multi-error fingerprint with one designated *primary* signature**; single-error cases are the degenerate case.
3. "Filtered log" = a relevant slice from the user; we **re-canonicalize regardless**.
4. A **wrong POA is worse than no POA** — incident responders act on these. Abstention is stricter than in generic RAG.

---

## 1. The biggest opinionated call: signatures are the engine, embeddings are the backstop

Generic RAG instinct says "embed everything, do vector search." For log↔log matching that instinct is **wrong as the primary mechanism**, and leaning on it is the most likely way to build something that demos well and fails in production. Reasons:

- Both sides are logs, so the highest-signal match is **error-template/error-code overlap**, which is exact-ish and explainable. `ECONNREFUSED to <IP>` in the query and in the case match because they canonicalize to the *same string*, not because two vectors are 0.91 cosine-similar.
- An embedding of a raw noisy log is dominated by noise; an embedding of a *canonical fingerprint* is better but still fuzzier and less auditable than a set-overlap score.
- In incident response, **"why did it match" is part of the answer.** "Matched on `pool exhausted` (primary) + `503 from upstream` (secondary)" is something an engineer trusts and verifies. "Cosine 0.88" is not.

So the architecture is: **a signature set-overlap leg as the workhorse, a vector leg as the fuzzy backstop for novel phrasings and partial matches, and a strict fusion + abstention layer on top.** Embeddings earn their place on the long tail, not the head. This also means **Phase 1 can ship signatures-only** and likely already be useful — vectors get added when the eval shows the fuzzy tail is worth it (consistent with v5's eval-gating philosophy).

---

## 2. The unit of ingestion: the Incident Case

A Case is not "a log file." It's a curated, deduplicated record:

- **Fingerprint** — the set of canonical error templates that identify this incident, with one marked `primary` and the rest `secondary`, each weighted.
- **POA** — the remediation: ordered steps, preconditions, rollback, owner, `last_verified_at`, optional cross-links to v5 guide sections.
- **Context** — service/component, environment, severity, version range where applicable.
- **Provenance & telemetry** — who authored it, when, how many times it's been seen/confirmed, when last confirmed helpful.

Two relationships the schema must support honestly because they're the hard cases:
- **N logs → 1 POA:** the same root cause surfaces in slightly different logs; all should resolve to one case (this is what canonicalization + fingerprinting buys you).
- **1 surface error → M POAs:** the same visible error has different root causes needing different fixes. This is the **disambiguation** problem (§7) and the reason a case is a *set* of signatures, not a single line — secondary signatures are what split a shared primary.

---

## 3. Ingestion pipeline (per case)

```
INTAKE  (CLI / resolution-hook / ticket import)
  raw triggering log + authored POA + context metadata
    → split headers/message, canonicalize via SHARED Drain3 masking config   [LIB+contract]
       → mine error-like lines → candidate templates                          [LIB]
          → author confirms/edits: pick PRIMARY, keep/drop SECONDARYs, weights [BUILD/UI]
             → build fingerprint (sorted canonical templates + match_text)     [BUILD]
                → DEDUP CHECK against existing cases (signature overlap)        [BUILD]  ← critical
                   ├─ strong overlap → propose MERGE / new-version of case
                   └─ no overlap     → new case
                      → redact secrets in log + POA (gitleaks)                 [LIB+GUARD]
                         → embed fingerprint bundle (local model, validated)   [LIB+GUARD]
                            → atomic commit: case + signatures + poa + vector   [BUILD]
                               → verify (signature round-trip, dim check)       [BUILD]
```

The stages that matter and why:

**Canonicalization is the same contract as the query side and as v5.** One Drain3 masking config, allowlist-first (HTTP codes, errno, SQLSTATE, vendor codes mask to themselves; volatile IP/TS/UUID/ID masked out, placeholders stripped for `match_text`). This is non-negotiable: if ingest and query canonicalize differently, nothing matches. It is a single versioned artifact imported by both pipelines.

**Author-in-the-loop fingerprinting is a feature, not friction.** Drain3 proposes the templates; the human who just solved the incident confirms the *primary* signature and prunes noise. Thirty seconds of curation here is worth more than any amount of downstream cleverness, because it fixes the label at the source. (If you go auto-harvest, this step becomes a batch heuristic + periodic human audit — strictly worse, see §9.)

**The dedup check is the single most important custom component.** Without it the case base fills with near-duplicate cases and retrieval returns five flavors of the same incident. On intake, compute signature overlap (Jaccard on the template set, primary-weighted) against active cases; above a threshold, propose merging into the existing case (incrementing `occurrence_count`, possibly versioning the POA) rather than creating a new row. This is the ingest-side mirror of the cross-group aggregation in v5 §9.4.

**Secrets redaction runs on both the log and the POA** — POAs routinely contain connection strings, internal hostnames, and example credentials. gitleaks rules, typed placeholders, before embedding or storage.

**Atomic commit + lifecycle** identical in spirit to v5: case + signatures + POA + embedding in one transaction; POA edits create a new POA version, old marked superseded; deactivation never deletes (you want the audit trail of "this fix used to be recommended"). Per-model embedding tables behind a default view, same atomic-cutover story for model upgrades.

---

## 4. What you embed (when you embed at all)

`case_text_for_embed` = a deterministic serialization of the fingerprint, **not** the raw log:

```
[service: payments-api] [severity: ERROR]
PRIMARY: connection pool exhausted, waited <NUM>ms for connection
SECONDARY: HTTP 503 from upstream <IP> after <NUM> retries
SECONDARY: circuit breaker open for <SERVICE>
```

Primary first, secondaries sorted for determinism, context prepended (the breadcrumb analogue). Query side builds the *identical* structure from the dropped log's top-ranked error groups. Because both sides are constructed the same way from the same canonicalizer, the embedding space is genuinely symmetric — this is why the vector leg works as a backstop here without query rewriting.

Optionally also embed the POA's **problem statement** (one human sentence: "what's actually wrong") in a separate column — useful only if you later add a free-text "describe your problem" query path. Don't build that until asked; the log is the query.

---

## 5. Database schema

Reuses the v5 Postgres + pgvector + pg_trgm instance. New table family:

```sql
-- The core unit: a deduplicated, curated incident case
CREATE TABLE incident_case (
    case_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title           TEXT NOT NULL,                       -- human label, e.g. "Payments pool exhaustion under burst load"
    service         TEXT,                                -- component/service tag; drives metadata filtering
    environment     TEXT,                                -- prod / staging / null
    severity        TEXT,                                -- FATAL/ERROR/WARN, the incident's worst
    status          TEXT NOT NULL DEFAULT 'active',      -- active | deprecated | draft
    version         INT  NOT NULL DEFAULT 1,
    occurrence_count INT NOT NULL DEFAULT 1,             -- how often this fingerprint has been seen/confirmed
    fingerprint_hash TEXT NOT NULL,                      -- hash of the sorted canonical template set; dedup key
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ,                         -- most recent confirmed occurrence (recency signal)
    text_for_embed  TEXT NOT NULL                        -- the serialization from §4
);
CREATE INDEX ON incident_case (service) WHERE status = 'active';
CREATE UNIQUE INDEX ON incident_case (fingerprint_hash) WHERE status = 'active';

-- The signatures that make up a case's fingerprint (the matching workhorse)
CREATE TABLE case_signature (
    signature_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id         BIGINT NOT NULL REFERENCES incident_case ON DELETE CASCADE,
    role            TEXT NOT NULL,                       -- 'primary' | 'secondary'
    weight          REAL NOT NULL DEFAULT 1.0,           -- author/eval-tuned salience
    template_raw    TEXT NOT NULL,                       -- example line as authored
    template_canon  TEXT NOT NULL,                       -- Drain3-masked, allowlist-preserved
    match_text      TEXT NOT NULL,                       -- canon with placeholder tokens stripped (trigram target)
    drain_cluster   TEXT                                 -- optional Drain3 cluster id for traceability
);
CREATE INDEX ON case_signature USING gin (match_text gin_trgm_ops);
CREATE INDEX ON case_signature (case_id);
CREATE INDEX ON case_signature (role);

-- The remediation. Separate table so a case can carry versioned/multiple POAs.
CREATE TABLE poa (
    poa_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id         BIGINT NOT NULL REFERENCES incident_case ON DELETE CASCADE,
    title           TEXT NOT NULL,
    problem_summary TEXT,                                -- one-sentence "what's wrong"
    steps           JSONB NOT NULL,                      -- ordered, structured steps (see note below)
    preconditions   TEXT,
    rollback        TEXT,
    guide_refs      JSONB,                               -- cross-links into the v5 guide index: [{document_id, section_path}]
    owner           TEXT,
    status          TEXT NOT NULL DEFAULT 'active',      -- active | superseded | deprecated
    version         INT  NOT NULL DEFAULT 1,
    last_verified_at TIMESTAMPTZ,                        -- staleness signal; surfaced to the engineer
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON poa (case_id) WHERE status = 'active';

-- Per-model embedding of the fingerprint bundle (backstop leg). Mirrors v5's per-model-table pattern.
CREATE TABLE case_embedding_v1 (                          -- name encodes model@version; new model = new table
    case_id         BIGINT PRIMARY KEY REFERENCES incident_case ON DELETE CASCADE,
    embedding       vector(1024) NOT NULL
);
CREATE INDEX ON case_embedding_v1 USING hnsw (embedding vector_cosine_ops);
-- A view `case_embedding_default` points at the current model's table; atomic cutover on upgrade.

-- Every time a case is matched/seen — telemetry, recency, and the audit trail
CREATE TABLE case_occurrence (
    occurrence_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id         BIGINT NOT NULL REFERENCES incident_case ON DELETE CASCADE,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT,                                -- 'query-confirmed' | 'reingested' | 'manual'
    raw_log_ref     TEXT,                                -- pointer to stored/redacted log, not the log inline
    confirmed       BOOLEAN                              -- did an engineer confirm this match was right?
);
CREATE INDEX ON case_occurrence (case_id, occurred_at DESC);

-- The flywheel: queries that found NO case. These are the next cases to author.
CREATE TABLE unresolved_query (
    query_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    fingerprint_hash TEXT,                               -- dedup unresolved queries too
    query_text_for_embed TEXT,                           -- so a future case can be seeded from it
    occurrence_count INT NOT NULL DEFAULT 1,             -- how many times this unsolved pattern recurred
    status          TEXT NOT NULL DEFAULT 'open'         -- open | promoted | dismissed
);
CREATE UNIQUE INDEX ON unresolved_query (fingerprint_hash) WHERE status = 'open';

-- Feedback on matches: drives eval and signature reweighting
CREATE TABLE match_feedback (
    feedback_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id         BIGINT REFERENCES incident_case,
    helpful         BOOLEAN NOT NULL,
    engineer        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Schema decisions worth defending:**
- `steps` as JSONB, not free text — lets the UI render checkboxes, lets you later detect "step references a deprecated tool," and keeps POAs structured enough to diff across versions. (If your POAs are genuinely prose, store markdown in a TEXT column instead — don't over-engineer.)
- `fingerprint_hash` as a unique partial index on active cases — the dedup guarantee lives in the database, not just application code.
- `case_occurrence` separate from the case — recency and frequency are *signals for ranking* and an audit trail; baking a counter into the case row alone loses the history you'll want for "is this case still relevant?"
- `unresolved_query` is not bookkeeping — it's the **product backlog of cases to write**, ranked by `occurrence_count`. The most-repeated unsolved incident is the highest-value case to author next.
- POA carries `last_verified_at` and `guide_refs` — staleness is shown to the engineer, and the two indexes (cases and guides) are linked rather than siloed.

---

## 6. Retrieval flow (so ingestion's choices make sense)

```
dropped log → split headers/message → Drain3 (stateless) → rank error groups (sev/recency/novelty)
  → build query fingerprint (primary + secondaries, §4 serialization)
     → LEG 1 (workhorse): signature set-overlap
          per query template: trigram match on case_signature.match_text
          aggregate to case score = Σ weight·sim, with a large multiplier when the PRIMARY matches a PRIMARY
     → LEG 2 (backstop): vector
          cosine(query fingerprint embedding, case_embedding_default), HNSW iterative scan
     → [LEG 3 optional: lexical for rare literal tokens not templated — add only if eval demands]
  → fuse (weighted RRF; signature leg weighted higher) → candidate cases
     → resolve to POA(s) per case
        → DECISION:
            score ≥ floor AND clear margin           → return POA + provenance + last_verified
            shared primary, secondaries disagree      → DISAMBIGUATE (§7)
            near-tie across different cases            → return both, status 'ambiguous'
            below floor                                → NO MATCH → log to unresolved_query → invite authoring
```

Provenance returned with every hit: which signatures matched (with counts from the log), the case title, POA `last_verified_at`, and any guide cross-links. The engineer should be able to see *why* in one glance and reject a bad match fast.

---

## 7. Disambiguation: the genuinely hard case, designed for explicitly

When several active cases share the same `primary` signature (same surface error, different root causes), do **not** silently pick the top-scored one. Instead:
1. Compute what *distinguishes* the candidate cases — the secondary signatures present in one case's fingerprint but not the others'.
2. Check the query for those distinguishing signatures. If present → that case wins decisively.
3. If the query lacks the distinguishing signals entirely → return the candidates as an explicit **decision tree**: "Same error, multiple known causes. If you also see `X` → POA-A; if `Y` → POA-B; if neither, here's how to tell them apart." 

This turns the system's weakest moment into a useful one: even without a confident single answer, it hands the engineer the exact discriminating questions. It's the case-based-reasoning equivalent of "differential diagnosis," and it's only possible *because* a case is a set of weighted signatures rather than one line.

---

## 8. The flywheel — this is the actual product, design it first

A `(log→POA)` DB starts empty and is useless until cases accrue; **cold start is the real risk, not retrieval quality.** The loop that fills it must be frictionless and self-reinforcing:

1. Query returns **no match** → fingerprint captured in `unresolved_query` (deduped, with a recurrence counter).
2. Engineer resolves the incident the hard way (possibly using the v5 guide system).
3. **One-click "promote to case"**: the captured fingerprint is pre-filled; the engineer writes the POA and confirms the primary signature. New case ingested.
4. Next occurrence of that fingerprint now matches.

Two consequences for the build order: the **intake path (CLI/hook/form) and the unresolved-query capture must exist in Phase 1**, before any sophisticated matching, because nothing else matters until cases exist. And `unresolved_query` ranked by `occurrence_count` becomes a standing "write these cases next" worklist — the system tells you where its own blind spots are.

---

## 9. If the pairs are auto-harvested instead of curated (the fork)

If POAs come from mining resolved tickets rather than author-in-the-loop:
- The dominant work moves to **extraction and cleaning**: parse ticket → find the attached/quoted log → find the resolution text → guess which is the POA. This is noisy NLP, not a transaction.
- **POA quality becomes the top risk.** Tickets contain "nvm restarted it, works now" non-POAs, wrong fixes, and resolutions that don't generalize. You'd need a quality gate (length, structure, presence of imperative steps) and a **human audit queue** before a harvested case goes `active` — harvested cases enter as `draft`, not `active`.
- Dedup pressure rises sharply (the same incident appears across many tickets), making §3's dedup check even more central.
- My recommendation: even with auto-harvest available, **gate everything through human confirmation before `active`.** A wrong POA acted on during an incident is a real outage amplifier. Harvest to *propose* cases; never to *publish* them. If volume makes that impractical, that's a signal to tighten the quality gate, not to drop the human.

This fork changes §3 (intake) and adds a draft/audit lifecycle to §5; it does **not** change the matching or schema core. Tell me which world we're in and I'll detail the harvest-and-clean sub-pipeline.

---

## 10. What to reuse vs build

| Concern | Decision | Note |
|---|---|---|
| Canonicalization (Drain3 + masking config) | **Reuse v5 contract verbatim** | The shared contract is what makes both indexes interoperate; one versioned config |
| Postgres + pgvector + pg_trgm | **Reuse the v5 instance** | New table family, same DB; cross-link POAs to guide sections |
| Embedding model | **Reuse the v5 pinned model** | Symmetric here, so no query rewrite needed; backstop role only |
| Secrets redaction (gitleaks) | **Reuse** | Now also over POA text |
| Lifecycle/versioning/atomic commit patterns | **Reuse the patterns** | Applied to cases + POAs; deactivate-don't-delete for audit |
| Eval harness | **Reuse, new golden set** | Golden set = (log → correct case) pairs + no-match logs + disambiguation cases |
| Signature set-overlap matching | **BUILD** (new) | The workhorse leg; doesn't exist in v5's asymmetric flow |
| Dedup-on-intake | **BUILD** (new, critical) | Prevents case-base rot |
| Disambiguation by distinguishing signatures | **BUILD** (new) | §7; the differentiator |
| Flywheel: unresolved-query capture + promote-to-case | **BUILD** (new, first) | §8; the actual product |
| Query rewrite / vocabulary-mismatch handling | **Drop** | Not needed — symmetric problem |

---

## 11. Build order

**Phase 0 — Golden set + schema.** ‹50–100› real (log → correct case) pairs, ‹10–20› no-match logs, ‹5–10› disambiguation cases (shared primary, different POA); the schema above; intake CLI; `unresolved_query` capture. Metric: case-level precision@1 and abstention precision (a wrong POA must cost more than a miss in the metric weighting).

**Phase 1 — Signatures-only, end to end.** Intake → canonicalize → author-confirm → dedup → store; retrieval = signature set-overlap leg + floor + unresolved capture + promote-to-case. **No embeddings yet.** This is a complete, useful flywheel. Measure precision@1 and abstention precision; this may already clear the bar for head incidents.

**Phase 2 — Vector backstop + disambiguation.** Add fingerprint embedding + vector leg + weighted fusion; add the disambiguation decision tree (§7). Measured by recall lift on the *fuzzy tail* the signature leg misses, and by disambiguation correctness on those golden cases. Vector leg ships only if it pays.

**Phase 3 — Ranking signals + feedback.** Fold recency (`last_seen_at`), frequency (`occurrence_count`), and `match_feedback` into ranking; signature reweighting from confirmed/rejected matches. Deprecation workflow for stale POAs (`last_verified_at` past a threshold → flag for re-verification).

**Deferred:** auto-harvest sub-pipeline (§9, only if that's the world), free-text "describe your problem" query path, cross-index unification UX with the v5 guide system.

---

## 12. Questions that would change this design

1. **Provenance of pairs** — curated-at-resolution (assumed) vs auto-harvested from tickets? Changes §3 and adds §9's draft/audit lifecycle. *Default: curated.*
2. **POA shape** — structured steps (assumed, JSONB) vs free-form prose (markdown TEXT)? Changes the `poa.steps` column and the UI. *Default: structured.*
3. **Is this replacing or complementing the v5 guide system?** I've designed it as **complementary and cross-linked** (a POA can point at guides). If it's meant to *replace* guide-RAG, say so — the calculus on what to build first changes. *Default: complementary.*
4. **Match granularity** — is one primary error usually enough to identify a case, or are most of your incidents genuinely multi-signal? Changes how hard to lean on disambiguation early. *Default: multi-signal, primary-anchored.*

Answer 1 and 3 and I can tighten the intake sub-pipeline and the cross-index UX into concrete detail.

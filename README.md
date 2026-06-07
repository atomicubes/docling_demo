# v5 Ingestion Demo — up to chunking

A simple, runnable slice of the ingestion pipeline from
`ingestion-flow-design-v5.md`, stopping at **chunking**. It produces a file of
chunks for analysis — **no embeddings, no storage, no retrieval**.

## Pipeline (matches §2 / §16 of the design)

```
discover & route by magic bytes        ingestion/discover.py      (§3, §16)
  → prose: Docling → DoclingDocument   ingestion/extract_prose.py (§4.1)
  → code:  tree-sitter + cAST          ingestion/chunk_code.py    (§4.2)
  → unified block/section model        ingestion/models.py        (§2)
  → secrets redaction                  ingestion/redact.py        (§4.3)
  → chunking + guards:
        prose: HybridChunker           ingestion/chunk_prose.py   (§5.1, §5.2)
        code:  cAST split/merge        ingestion/chunk_code.py    (§4.2)
  → write chunks to output file        ingest.py
```

Libraries are the ones named in the design: **Docling** (prose extraction +
HybridChunker), **tree-sitter** (code parsing for the cAST chunker), and the
**bge-m3** tokenizer (a Phase-0 candidate, §7) used only to *size* chunks — it
is the chunker's token counter, not an embedding step.

### Deliberately skipped for the demo

- **LibreOffice pre-pass** (§3 / §4.1) → so legacy `.doc` and tracked-changes
  flattening are out; Docling handles docx/md/txt/html/pdf directly.
- Embedding, storage (Postgres/pgvector), and the entire retrieval side.
- gitleaks is approximated by a small regex ruleset in `redact.py` (same
  behaviour: typed placeholders, per-file counts, `redacted` flag).

## Run

```bash
uv sync
uv run ingest.py sample_docs --out output/chunks.jsonl
```

Each line of `output/chunks.jsonl` is one chunk:

```json
{
  "doc_id": "…", "source_path": "…", "format": "prose|code",
  "section_path": ["H1", "H2", …],
  "text": "display text",
  "text_for_embed": "[breadcrumb]\n…",   // §5.2 breadcrumb-prefixed
  "token_count": 418,
  "language": "python", "start_line": 1, "end_line": 47,   // code provenance §4.2
  "redactions": 1, "quality_flags": ["redacted", "undersized", …]
}
```

`quality_flags` come from the guard layer (§5.1): `undersized`, `over_max_tokens`,
`no_section` for prose; `preamble`, `low_quality_structure` for code.

## Sample corpus (`sample_docs/`)

- `connection_pool_guide.md` — prose guide with headings, a table, and code blocks
- `restart_runbook.txt` — plain-text guide (no headings → `no_section` flag)
- `pool_manager.py` — Python with classes/docstrings + two fake credentials
- `healthcheck.sh` — shell script + a fake credential

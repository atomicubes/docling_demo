"""Postgres access layer. All SQL lives here.

Uses psycopg3 with a connection pool. The schema is applied idempotently at
startup so a fresh `docker compose up` is all that's needed.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from psycopg_pool import ConnectionPool

from .config import settings
from .normalizer import NORMALIZER_VERSION

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS poa (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    steps_md    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signature (
    id                  SERIAL PRIMARY KEY,
    sig_hash            CHAR(64) NOT NULL,
    sig_text            TEXT NOT NULL,
    raw_example         TEXT NOT NULL,
    normalizer_version  INT  NOT NULL,
    poa_id              INT  NOT NULL REFERENCES poa(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sig_hash, poa_id)
);

CREATE INDEX IF NOT EXISTS idx_signature_hash ON signature (sig_hash);
CREATE INDEX IF NOT EXISTS idx_signature_trgm
    ON signature USING gin (sig_text gin_trgm_ops);

-- Queries that matched nothing: the curation/flywheel queue.
CREATE TABLE IF NOT EXISTS unmatched_query (
    id          SERIAL PRIMARY KEY,
    sig_text    TEXT NOT NULL,
    raw_record  TEXT NOT NULL,
    seen_count  INT NOT NULL DEFAULT 1,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sig_text)
);
"""

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=8, open=True
        )
    return _pool


@contextmanager
def get_conn() -> Iterator[Any]:
    with get_pool().connection() as conn:
        yield conn


def init_schema() -> None:
    with get_conn() as conn:
        conn.execute(SCHEMA_SQL)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# POA / signature writes
# ---------------------------------------------------------------------------

def insert_poa(title: str, steps_md: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO poa (title, steps_md) VALUES (%s, %s) RETURNING id",
            (title, steps_md),
        ).fetchone()
        return int(row[0])


def insert_signature(
    sig_hash: str, sig_text: str, raw_example: str, poa_id: int
) -> int | None:
    """Insert a signature; returns its id, or None if it already existed."""
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO signature
                (sig_hash, sig_text, raw_example, normalizer_version, poa_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (sig_hash, poa_id) DO NOTHING
            RETURNING id
            """,
            (sig_hash, sig_text, raw_example, NORMALIZER_VERSION, poa_id),
        ).fetchone()
        return int(row[0]) if row else None


def get_poa(poa_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, steps_md FROM poa WHERE id = %s", (poa_id,)
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "title": row[1], "steps_md": row[2]}


# ---------------------------------------------------------------------------
# Matching reads
# ---------------------------------------------------------------------------

def lookup_exact(hashes: list[str]) -> dict[str, dict]:
    """Batch exact-hash lookup. Returns {sig_hash: signature row}."""
    if not hashes:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.sig_hash, s.sig_text, s.raw_example, s.poa_id,
                   p.title
            FROM signature s
            JOIN poa p ON p.id = s.poa_id
            WHERE s.sig_hash = ANY(%s)
            """,
            (hashes,),
        ).fetchall()
    return {
        r[0]: {
            "sig_text": r[1],
            "raw_example": r[2],
            "poa_id": r[3],
            "poa_title": r[4],
        }
        for r in rows
    }


def lookup_fuzzy(sig_text: str, threshold: float) -> dict | None:
    """Best trigram match above threshold, or None."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT s.sig_text, s.raw_example, s.poa_id, p.title,
                   similarity(s.sig_text, %s) AS sim
            FROM signature s
            JOIN poa p ON p.id = s.poa_id
            WHERE similarity(s.sig_text, %s) > %s
            ORDER BY sim DESC
            LIMIT 1
            """,
            (sig_text, sig_text, threshold),
        ).fetchone()
    if not row:
        return None
    return {
        "sig_text": row[0],
        "raw_example": row[1],
        "poa_id": row[2],
        "poa_title": row[3],
        "similarity": float(row[4]),
    }


def record_unmatched(sig_text: str, raw_record: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO unmatched_query (sig_text, raw_record)
            VALUES (%s, %s)
            ON CONFLICT (sig_text) DO UPDATE
                SET seen_count = unmatched_query.seen_count + 1,
                    last_seen  = now()
            """,
            (sig_text, raw_record),
        )


def list_unmatched(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, sig_text, raw_record, seen_count, last_seen
            FROM unmatched_query
            ORDER BY seen_count DESC, last_seen DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "sig_text": r[1],
            "raw_record": r[2],
            "seen_count": r[3],
            "last_seen": r[4].isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Reindex (after a NORMALIZER_VERSION bump)
# ---------------------------------------------------------------------------

def all_signatures_for_reindex() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, raw_example, normalizer_version FROM signature"
        ).fetchall()
    return [
        {"id": r[0], "raw_example": r[1], "normalizer_version": r[2]}
        for r in rows
    ]


def update_signature_canonical(
    sig_id: int, sig_hash: str, sig_text: str
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE signature
            SET sig_hash = %s, sig_text = %s, normalizer_version = %s
            WHERE id = %s
            """,
            (sig_hash, sig_text, NORMALIZER_VERSION, sig_id),
        )

"""HTTP API.

Endpoints:
  GET  /health                liveness + schema check
  POST /incidents             ingest one curated (logs -> POA) incident
  POST /analyze               raw log text in -> POA out (the core contract)
  POST /analyze/file          same, multipart file upload
  GET  /poa/{id}              fetch a stored POA
  GET  /unmatched             curation queue (the flywheel)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import db
from .config import settings
from .ingest import ingest_incident
from .matcher import analyze_text
from .normalizer import NORMALIZER_VERSION
from .presenter import render


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_schema()
    yield
    db.close_pool()


app = FastAPI(
    title="log2poa",
    description="Raw logs in, Plan of Action out.",
    version="0.1.0",
    lifespan=lifespan,
)


class IncidentIn(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    steps_md: str = Field(min_length=10)
    symptom_logs: str = Field(min_length=1)


class AnalyzeIn(BaseModel):
    logs: str = Field(min_length=1)


@app.get("/health")
def health() -> dict:
    db.init_schema()  # idempotent; doubles as a connectivity check
    return {"status": "ok", "normalizer_version": NORMALIZER_VERSION}


@app.post("/incidents", status_code=201)
def create_incident(body: IncidentIn) -> dict:
    try:
        report = ingest_incident(body.title, body.steps_md, body.symptom_logs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return asdict(report)


def _analyze(raw: str) -> dict:
    result = analyze_text(raw)
    presentation = render(result, raw_excerpt=raw[:4000])
    return {
        "verdict": result.verdict,
        "confidence": result.confidence,
        "records_analyzed": result.records_analyzed,
        "candidates": [
            {
                "poa_id": c.poa_id,
                "title": c.title,
                "score": round(c.score, 3),
                "exact_hits": c.exact_hits,
                "fuzzy_hits": c.fuzzy_hits,
                "evidence": [
                    {
                        "user_line": e.user_line,
                        "signature": e.signature,
                        "match_type": e.match_type,
                        "matched_example": e.matched_example,
                        "similarity": e.similarity,
                    }
                    for e in c.evidence
                ],
            }
            for c in result.candidates[:5]
        ],
        "unmatched_signatures": [e.signature for e in result.unmatched],
        **presentation,
    }


@app.post("/analyze")
def analyze(body: AnalyzeIn) -> dict:
    if len(body.logs.encode()) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Log payload too large.")
    return _analyze(body.logs)


@app.post("/analyze/file")
async def analyze_file(file: UploadFile = File(...)) -> dict:
    raw = await file.read()
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Log file too large.")
    return _analyze(raw.decode("utf-8", errors="replace"))


@app.get("/poa/{poa_id}")
def get_poa(poa_id: int) -> dict:
    poa = db.get_poa(poa_id)
    if not poa:
        raise HTTPException(status_code=404, detail="POA not found")
    return poa


@app.get("/unmatched")
def unmatched(limit: int = 50) -> list[dict]:
    return db.list_unmatched(min(max(limit, 1), 200))

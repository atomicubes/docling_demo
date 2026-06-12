"""Presentation layer.

Two modes:
  * Deterministic (default): renders the stored POA with the evidence table.
    Always available, zero external dependencies — the POC works offline.
  * LLM (optional): if ANTHROPIC_API_KEY is set, Claude rewrites the response
    grounded in the user's actual log lines (real port numbers, real paths).
    The stored POA remains the source of truth; the model is instructed to
    adapt, never invent, remediation steps. Any LLM failure falls back to the
    deterministic rendering — presentation must never break a query.
"""

from __future__ import annotations

from . import db
from .config import settings
from .matcher import MatchResult


def _evidence_block(result: MatchResult) -> str:
    lines = []
    top = result.candidates[0]
    for ev in top.evidence:
        tag = (
            "exact match"
            if ev.match_type == "exact"
            else f"close match ({ev.similarity:.2f})"
        )
        lines.append(f"- `{ev.user_line.strip()}`")
        lines.append(f"  matched documented line `{ev.matched_example}` ({tag})")
    return "\n".join(lines)


def render_deterministic(result: MatchResult) -> str:
    if result.verdict == "no_errors":
        return (
            "No error or warning records were found in the supplied logs. "
            "Nothing to diagnose."
        )

    if result.verdict == "no_match":
        n = len(result.unmatched)
        return (
            f"**Unknown error — no documented POA.**\n\n"
            f"{n} error signature(s) were extracted but none matched the "
            f"knowledge base (exactly or approximately). They have been "
            f"queued for curation. Signatures:\n\n"
            + "\n".join(f"- `{e.signature}`" for e in result.unmatched)
        )

    top = result.candidates[0]
    poa = db.get_poa(top.poa_id)
    steps = poa["steps_md"] if poa else "(POA body missing)"

    if result.verdict == "candidates":
        others = "\n".join(
            f"- {c.title} (score {c.score:.2f}, "
            f"{c.exact_hits} exact / {c.fuzzy_hits} fuzzy)"
            for c in result.candidates[:3]
        )
        return (
            f"**Multiple plausible diagnoses — confidence too low to pick one "
            f"(confidence {result.confidence:.2f}).**\n\n"
            f"Candidates, ranked:\n{others}\n\n"
            f"Top candidate's plan is shown below; verify the evidence before "
            f"acting.\n\n---\n\n## {top.title}\n\n{steps}\n\n"
            f"### Evidence\n{_evidence_block(result)}"
        )

    header = (
        f"## {top.title}\n\n"
        f"Confidence {result.confidence:.2f} — "
        f"{top.exact_hits} exact and {top.fuzzy_hits} close signature "
        f"match(es) out of {result.records_analyzed} analyzed.\n"
    )
    unmatched_note = ""
    if result.unmatched:
        unmatched_note = (
            f"\n\nNote: {len(result.unmatched)} additional error signature(s) "
            f"did not match any documented incident and were queued for review."
        )
    return (
        f"{header}\n{steps}\n\n### Evidence\n{_evidence_block(result)}"
        f"{unmatched_note}"
    )


_LLM_SYSTEM = """You are the presentation layer of an incident-response tool.
You receive (a) a documented Plan of Action and (b) the user's actual error
log lines that matched it. Rewrite the POA as a clear response for the
engineer, substituting concrete values from THEIR logs (real ports, paths,
hosts) where the POA is generic.

Hard rules:
- The POA is the source of truth. NEVER invent, add, or reorder remediation
  steps. You may only rephrase and substitute concrete values.
- Quote the user's matching log lines as evidence.
- State the confidence figure you are given verbatim.
- If the match type is 'fuzzy', say the match is approximate and advise
  verifying before acting."""


def render_with_llm(result: MatchResult, raw_excerpt: str) -> str | None:
    """Returns contextualized text, or None to signal deterministic fallback."""
    if not settings.anthropic_api_key or result.verdict not in ("match", "candidates"):
        return None
    try:
        import anthropic

        top = result.candidates[0]
        poa = db.get_poa(top.poa_id)
        if not poa:
            return None
        evidence = "\n".join(
            f"[{e.match_type}] user line: {e.user_line}"
            for e in top.evidence
        )
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1200,
            system=_LLM_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Confidence: {result.confidence:.2f}\n"
                        f"POA title: {poa['title']}\n"
                        f"POA steps (source of truth):\n{poa['steps_md']}\n\n"
                        f"Matched evidence:\n{evidence}\n\n"
                        f"Excerpt of the user's raw logs:\n{raw_excerpt[:4000]}"
                    ),
                }
            ],
        )
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        return "\n".join(parts) if parts else None
    except Exception:  # noqa: BLE001 - presentation must never fail the query
        return None


def render(result: MatchResult, raw_excerpt: str = "") -> dict:
    llm_text = render_with_llm(result, raw_excerpt)
    return {
        "rendered": llm_text or render_deterministic(result),
        "renderer": "llm" if llm_text else "deterministic",
    }

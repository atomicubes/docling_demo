"""Code chunking — [BUILD] cAST + our invariants, on tree-sitter (§4.2).

cAST (Zhang et al.): recursively split oversized AST nodes, greedily merge small
sibling nodes under the token budget. On top we layer the demo subset of the
v5 invariants:

  * file preamble (imports/includes) emitted as one small chunk;
  * signature (+ python docstring) repeated on every sub-chunk of an oversized
    definition, replacing token overlap;
  * start_line / end_line provenance on every chunk;
  * minified / unsplittable code → token-window split + `low_quality_structure`.

`.h ↔ .c` linking and per-language astchunk swap (§4.2) are out of demo scope.

The tree-sitter binding shipped via tree-sitter-language-pack exposes a
method-based Node API (`node.kind()`, `node.child(i)`, `node.start_byte()`,
byte offsets into UTF-8), so a thin adapter at the top keeps the cAST logic
readable.
"""

from __future__ import annotations

from pathlib import Path

from .models import Chunk
from .tokenizer import MAX_TOKENS, count_tokens

# node kinds that begin a file preamble (imports / includes / using / package)
_PREAMBLE = ("import", "include", "preproc", "using", "package")
# block/body node kinds whose start marks the end of a definition's signature
_BODY = ("block", "suite", "compound_statement", "statement_block", "body")

_LANG_TS = {
    "python": "python",
    "csharp": "csharp",
    "bash": "bash",
    "c": "c",
    "cpp": "cpp",
}


# ---- thin adapter over the method-based Node API ----------------------------
def _kids(node) -> list:
    return [node.child(i) for i in range(node.child_count())]


def _named_kids(node) -> list:
    return [c for c in _kids(node) if c.is_named()]


def _kind(node) -> str:
    return node.kind()


def _txt(node, src: bytes) -> str:
    return src[node.start_byte():node.end_byte()].decode("utf-8", "replace")


def _lines(node) -> tuple[int, int]:
    return node.start_position().row + 1, node.end_position().row + 1


# ---- cAST + invariants ------------------------------------------------------
def _signature(node, src: bytes) -> str:
    """Text from the node start up to its body block — the def's signature.
    For python, also fold in a leading docstring (§4.2 invariant)."""
    body = next((c for c in _kids(node) if any(b in _kind(c) for b in _BODY)), None)
    if body is None:
        return _txt(node, src).splitlines()[0]
    sig = src[node.start_byte():body.start_byte()].decode("utf-8", "replace").rstrip()

    # python docstring = first string expression statement inside the body
    for stmt in _kids(body):
        if _kind(stmt) == "expression_statement":
            inner = _kids(stmt)
            if inner and _kind(inner[0]) == "string":
                sig = sig + "\n" + _txt(inner[0], src)
        if _kind(stmt) not in ("comment",):
            break
    return sig


def _token_window_split(text: str, header: str = "") -> list[str]:
    """Last-resort split for unsplittable / minified nodes."""
    words = text.split()
    out, buf = [], []
    budget = MAX_TOKENS - count_tokens(header) if header else MAX_TOKENS
    for w in words:
        buf.append(w)
        if count_tokens(" ".join(buf)) > budget:
            buf.pop()
            out.append(" ".join(buf))
            buf = [w]
    if buf:
        out.append(" ".join(buf))
    return [(header + "\n" + p if header else p) for p in out]


def _split_oversized(node, src: bytes) -> list[tuple[str, int, int, list[str]]]:
    """Recursively split a node that alone exceeds the budget, repeating the
    signature on each piece. Returns (text, start_line, end_line, flags)."""
    body = next((c for c in _kids(node) if any(b in _kind(c) for b in _BODY)), None)
    sig = _signature(node, src)

    if body is None or not _named_kids(body):
        # nothing to recurse into → minified / single huge expression
        s, e = _lines(node)
        return [(t, s, e, ["low_quality_structure"])
                for t in _token_window_split(_txt(node, src))]

    return _chunk_siblings(_named_kids(body), src, header=sig)


def _chunk_siblings(nodes, src: bytes, header: str = "") \
        -> list[tuple[str, int, int, list[str]]]:
    """Greedy cAST merge of sibling nodes under the budget; recurse on
    oversized ones. `header` (a signature) is prepended to every piece."""
    out: list[tuple[str, int, int, list[str]]] = []
    buf: list = []

    def flush():
        if not buf:
            return
        text = "\n".join(_txt(n, src) for n in buf)
        if header:
            text = header + "\n" + text
        out.append((text, _lines(buf[0])[0], _lines(buf[-1])[1], []))
        buf.clear()

    hdr_tokens = count_tokens(header) if header else 0
    for n in nodes:
        ntok = count_tokens(_txt(n, src))
        if hdr_tokens + ntok > MAX_TOKENS:
            flush()
            out.extend(_split_oversized(n, src))
        elif sum(count_tokens(_txt(b, src)) for b in buf) + ntok + hdr_tokens > MAX_TOKENS:
            flush()
            buf.append(n)
        else:
            buf.append(n)
    flush()
    return out


def chunk_code(path: Path, language: str, doc_id: str) -> list[Chunk]:
    from tree_sitter_language_pack import get_parser

    src = path.read_bytes()
    parser = get_parser(_LANG_TS.get(language, language))
    tree = parser.parse(src.decode("utf-8", "replace"))
    top = _named_kids(tree.root_node())

    chunks: list[Chunk] = []

    # 1) file preamble (imports/includes/license) as one small chunk (§4.2)
    preamble, rest = [], []
    leading = True
    for n in top:
        if leading and any(p in _kind(n) for p in _PREAMBLE):
            preamble.append(n)
        else:
            leading = False
            rest.append(n)

    def emit(text, s, e, flags):
        chunks.append(Chunk(
            doc_id=doc_id, source_path=str(path), format="code", kind="code",
            section_path=[path.name], text=text, text_for_embed=text,
            token_count=count_tokens(text), language=language,
            start_line=s, end_line=e, quality_flags=list(flags),
        ))

    if preamble:
        text = "\n".join(_txt(n, src) for n in preamble)
        emit(text, _lines(preamble[0])[0], _lines(preamble[-1])[1], ["preamble"])

    # 2) cAST split/merge over the remaining top-level definitions
    for text, s, e, flags in _chunk_siblings(rest, src):
        emit(text, s, e, flags)

    return chunks

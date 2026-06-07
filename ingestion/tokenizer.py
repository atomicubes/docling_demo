"""The shared tokenizer (§5, §7).

The design says chunk sizes are measured with the *winning embedding model's
actual tokenizer*, and that same tokenizer is reused by the guard layer and the
code chunker so every stage agrees on "how big is this". For the demo we use
bge-m3, one of the two named Phase-0 bake-off candidates.
"""

from __future__ import annotations

from functools import lru_cache

# Phase-0 candidate (§7). In the real air-gapped bundle these weights are
# vendored; here transformers fetches the tokenizer once and caches it.
EMBED_MODEL = "BAAI/bge-m3"

# max_tokens = 512 − breadcrumb budget − 10% pad  (§5.1)
EMBED_CTX = 512
BREADCRUMB_BUDGET = 40
PAD = int(EMBED_CTX * 0.10)
MAX_TOKENS = EMBED_CTX - BREADCRUMB_BUDGET - PAD   # ~ 421
MIN_TOKENS = 60                                    # merge floor (§5.1)


@lru_cache(maxsize=1)
def _tok():
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(EMBED_MODEL)


def count_tokens(text: str) -> int:
    return len(_tok().encode(text, add_special_tokens=False))


def hf_tokenizer():
    """The raw HF tokenizer, for Docling's HybridChunker."""
    return _tok()

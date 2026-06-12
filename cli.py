"""CLI: seed the knowledge base and run maintenance tasks.

Usage:
    python cli.py seed seed/seed_example.yaml
    python cli.py reindex
    python cli.py normalize "some raw log line"   # debug a signature
"""

from __future__ import annotations

import sys

import yaml

from app import db
from app.ingest import ingest_incident, reindex_all
from app.normalizer import NORMALIZER_VERSION, normalize, signature_hash


def cmd_seed(path: str) -> None:
    db.init_schema()
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    incidents = data.get("incidents", [])
    if not incidents:
        print("No incidents found in seed file.")
        return
    for inc in incidents:
        report = ingest_incident(
            inc["title"], inc["steps_md"], inc["symptom_logs"]
        )
        print(
            f"POA #{report.poa_id} '{inc['title']}': "
            f"{report.signatures_added} signature(s) added, "
            f"{report.signatures_skipped} duplicate(s) skipped."
        )


def cmd_reindex() -> None:
    db.init_schema()
    n = reindex_all()
    print(f"Reindexed {n} signature(s) at normalizer v{NORMALIZER_VERSION}.")


def cmd_normalize(line: str) -> None:
    sig = normalize(line)
    print(f"signature: {sig}")
    print(f"hash:      {signature_hash(sig)}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "seed" and args:
        cmd_seed(args[0])
    elif cmd == "reindex":
        cmd_reindex()
    elif cmd == "normalize" and args:
        cmd_normalize(" ".join(args))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

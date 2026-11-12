"""Command-line entry point: `au-ledger ingest <drop>...` and `au-ledger report --out <dir>`."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .db import check_key_fingerprint, connect
from .ingest import IngestError, Ingestor
from .pii import PiiVault
from .report import build_reports
from .schema import load_dispositions, load_schemas

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMAS = REPO_ROOT / "assignment-a-data" / "schemas"
DEFAULT_DB = REPO_ROOT / "solution" / "var" / "ledger.sqlite3"
DEFAULT_KEY = REPO_ROOT / "solution" / "var" / "pii.key"


def _open(args):
    vault = PiiVault.load(args.key_file)
    conn = connect(args.db)
    check_key_fingerprint(conn, vault.fingerprint)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM dispositions")
    conn.executemany("INSERT INTO dispositions VALUES (?, ?, ?, ?, ?)",
                     load_dispositions(args.schemas / "dispositions.csv"))
    conn.execute("COMMIT")
    return conn, vault


def cmd_ingest(args) -> int:
    conn, vault = _open(args)
    t0 = time.perf_counter()
    try:
        summary = Ingestor(conn, load_schemas(args.schemas), vault).ingest_drops(args.drops)
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for note in summary.notes:
        print(f"warning: {note}", file=sys.stderr)
    print(f"ingested {summary.files_ingested} file(s), skipped {summary.files_skipped} already-ingested; "
          f"{summary.lead_rows} lead rows, {summary.events} event lines "
          f"in {time.perf_counter() - t0:.2f}s")
    return 0


def cmd_report(args) -> int:
    conn, _ = _open(args)
    t0 = time.perf_counter()
    build_reports(conn, args.out)
    print(f"wrote 5 reports to {args.out}/ in {time.perf_counter() - t0:.2f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="au-ledger", description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite file (default {DEFAULT_DB})")
    p.add_argument("--schemas", type=Path, default=DEFAULT_SCHEMAS, help="schema directory")
    p.add_argument("--key-file", type=Path, default=DEFAULT_KEY,
                   help=f"PII master key file, created if missing (default {DEFAULT_KEY}); "
                        "AU_PII_KEY env var takes precedence")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", help="ingest one or more drops, in the order given")
    pi.add_argument("drops", type=Path, nargs="+",
                    help="a drop directory, or a directory of drop_* directories")
    pi.set_defaults(func=cmd_ingest)
    pr = sub.add_parser("report", help="write the five report files")
    pr.add_argument("--out", type=Path, required=True)
    pr.set_defaults(func=cmd_report)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

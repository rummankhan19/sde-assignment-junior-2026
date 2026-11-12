import csv
import json
from pathlib import Path

import pytest

from au_ledger.cli import DEFAULT_SCHEMAS, main

PHONE_A, PHONE_B, PHONE_C = "9876500001", "9876500002", "9876500003"
MOTOR_HEADER = ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount",
                "preferred_language"]


def write_leads(drop: Path, workflow: str, batch: str, header: list[str], rows: list[list[str]]):
    p = drop / "leads" / workflow / f"{batch}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def write_events(drop: Path, name: str, events: list):
    """`events` items are dicts (serialised) or raw strings (written verbatim)."""
    p = drop / "events" / f"{name}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n")


class Ev:
    """Tiny event factory: Ev().initiated("c1", PHONE_A, "2026-10-01T10:00:00+05:30")."""

    def __init__(self, workflow="motor_renewal"):
        self.wf = workflow
        self.n = 0

    def make(self, etype, call, phone, at, payload=None):
        self.n += 1
        e = {"event_id": f"evt_{self.n:05d}", "event_type": etype, "workflow_id": self.wf,
             "call_id": call, "occurred_at": at, "payload": payload or {}}
        if phone is not None:
            e["phone"] = phone
        return e

    def initiated(self, call, phone, at):
        return self.make("call.initiated", call, phone, at)

    def connected(self, call, phone, at):
        return self.make("call.connected", call, phone, at)

    def failed(self, call, phone, at):
        return self.make("call.telephony_failed", call, phone, at, {"reason": "sip_503"})

    def disp(self, call, phone, at, code):
        return self.make("disposition.set", call, phone, at, {"disposition_code": code})

    def override(self, call, at, code):
        return self.make("disposition.override", call, None, at,
                         {"disposition_code": code, "overridden_by": "sup", "reason": "qa_review"})


@pytest.fixture
def ledger(tmp_path):
    """Returns run(*argv) bound to a fresh database, plus a helper to read reports."""
    db, key = tmp_path / "db.sqlite3", tmp_path / "pii.key"

    class Ledger:
        root = tmp_path

        def run(self, *argv):
            assert main(["--db", str(db), "--key-file", str(key), "--schemas", str(DEFAULT_SCHEMAS),
                         *map(str, argv)]) == 0

        def ingest(self, *drops):
            self.run("ingest", *drops)

        def report(self, name="out"):
            out = tmp_path / name
            self.run("report", "--out", out)
            return Reports(out)

        db_path = db

    return Ledger()


class Reports:
    def __init__(self, out: Path):
        self.out = out

    def csv(self, name):
        with (self.out / f"{name}.csv").open(newline="") as fh:
            return list(csv.DictReader(fh))

    def leads(self):
        return {r["external_ref"]: r for r in self.csv("lead_report")}

    @property
    def quality(self):
        return json.loads((self.out / "data_quality.json").read_text())

    def snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.out.iterdir())}

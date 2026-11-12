"""Ingest one or more drops. Each drop is applied in a single transaction: a crash mid-drop
leaves the database exactly as it was, and re-running the drop is then safe."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalise_phone, parse_timestamp, to_epoch_us
from .pii import PiiVault
from .schema import WorkflowSchema, validate_row


class IngestError(Exception):
    pass


@dataclass
class IngestSummary:
    files_ingested: int = 0
    files_skipped: int = 0
    lead_rows: int = 0
    events: int = 0
    notes: list[str] = field(default_factory=list)


def _natural_key(p: Path):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def expand_drops(paths: list[Path]) -> list[Path]:
    """A path is a drop if it has leads/ or events/. Otherwise its drop_* children are used,
    in natural order (drop_2 before drop_10)."""
    drops = []
    for p in paths:
        if not p.is_dir():
            raise IngestError(f"{p} is not a directory")
        if (p / "leads").is_dir() or (p / "events").is_dir():
            drops.append(p)
        else:
            children = sorted((c for c in p.iterdir() if c.is_dir() and c.name.startswith("drop")),
                              key=_natural_key)
            if not children:
                raise IngestError(f"{p} contains no leads/, events/ or drop_* directories")
            drops.extend(children)
    return drops


class Ingestor:
    def __init__(self, conn: sqlite3.Connection, schemas: dict[str, WorkflowSchema], vault: PiiVault):
        self.conn = conn
        self.schemas = schemas
        self.vault = vault
        self._lead_cache: dict[str, dict[str, dict]] = {}

    # ------------------------------------------------------------------ drops
    def ingest_drops(self, paths: list[Path]) -> IngestSummary:
        summary = IngestSummary()
        for drop in expand_drops(paths):
            self.conn.execute("BEGIN IMMEDIATE")
            # Reload leads under the write lock: another process may have committed leads
            # since our last drop, and a stale cache would try to insert them again.
            self._lead_cache.clear()
            try:
                self._ingest_drop(drop, summary)
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return summary

    def _ingest_drop(self, drop: Path, summary: IngestSummary) -> None:
        leads_dir = drop / "leads"
        if leads_dir.is_dir():
            for wf_dir in sorted(d for d in leads_dir.iterdir() if d.is_dir()):
                if wf_dir.name not in self.schemas:
                    summary.notes.append(f"skipped {wf_dir}: no schema for workflow '{wf_dir.name}'")
                    continue
                for f in sorted(wf_dir.glob("*.csv")):
                    self._ingest_lead_file(wf_dir.name, f, summary)
        events_dir = drop / "events"
        if events_dir.is_dir():
            for f in sorted(events_dir.glob("*.jsonl")):
                self._ingest_event_file(f, summary)

    def _register_file(self, kind: str, path: Path, data: bytes, summary: IngestSummary,
                       workflow_id: str | None = None, batch_id: str | None = None) -> int | None:
        sha = hashlib.sha256(data).hexdigest()
        if self.conn.execute(
                "SELECT 1 FROM ingested_files WHERE kind = ? AND COALESCE(workflow_id, '') = ? "
                "AND file_name = ? AND sha256 = ?",
                (kind, workflow_id or "", path.name, sha)).fetchone():
            summary.files_skipped += 1
            return None
        if kind == "leads":
            clash = self.conn.execute(
                "SELECT source_path FROM ingested_files WHERE kind = 'leads' AND workflow_id = ? "
                "AND batch_id = ?", (workflow_id, batch_id)).fetchone()
            if clash:
                raise IngestError(
                    f"{path}: batch {workflow_id}/{batch_id} was already ingested from "
                    f"{clash['source_path']} with different content. Batch ids are permanent; "
                    "re-upload corrections under a new batch name.")
        cur = self.conn.execute(
            "INSERT INTO ingested_files (kind, file_name, sha256, source_path, workflow_id, batch_id) "
            "VALUES (?, ?, ?, ?, ?, ?)", (kind, path.name, sha, str(path), workflow_id, batch_id))
        summary.files_ingested += 1
        return cur.lastrowid

    # ------------------------------------------------------------------ leads
    def _leads_for(self, workflow_id: str) -> dict[str, dict]:
        if workflow_id not in self._lead_cache:
            self._lead_cache[workflow_id] = {
                r["phone_hash"]: {"lead_id": r["lead_id"], "attrs": json.loads(r["attrs"]),
                                  "pii": json.loads(r["pii_enc"]), "ref": r["external_ref"]}
                for r in self.conn.execute(
                    "SELECT lead_id, phone_hash, attrs, pii_enc, external_ref FROM leads "
                    "WHERE workflow_id = ?", (workflow_id,))}
        return self._lead_cache[workflow_id]

    def _ingest_lead_file(self, workflow_id: str, path: Path, summary: IngestSummary) -> None:
        data = path.read_bytes()
        batch_id = path.stem
        file_id = self._register_file("leads", path, data, summary, workflow_id, batch_id)
        if file_id is None:
            return
        schema = self.schemas[workflow_id]
        pii_fields = {f.name for f in schema.fields if f.pii}
        phone_field, ref_field = schema.phone_field, schema.reference_field
        leads = self._leads_for(workflow_id)

        reader = csv.reader(io.StringIO(_decode_csv(data, path, summary), newline=""))
        header = next(reader, None) or []
        colmap = schema.map_headers(header)

        total = rejected = merged = warnings = 0
        rejections, touched = [], {}
        for cells in reader:
            if all(not c.strip() for c in cells):
                continue  # blank rows are skipped and not counted
            total += 1
            raw = {name: cells[i] for i, name in colmap.items() if i < len(cells)}
            res = validate_row(schema, raw)
            if res.reason:
                rejected += 1
                rejections.append((workflow_id, batch_id, total, res.reason, file_id))
                continue
            warnings += res.warnings
            phone = res.values[phone_field]
            key = self.vault.blind_index(phone)
            present = {k: v for k, v in res.values.items() if v is not None}
            lead = leads.get(key)
            if lead is None:
                lead = {"lead_id": None, "attrs": {}, "pii": {}, "ref": None,
                        "batch_id": batch_id, "last4": phone[-4:]}
                leads[key] = lead
            else:
                merged += 1
            for name, value in present.items():  # last non-null write wins
                if name in pii_fields:
                    lead["pii"][name] = self.vault.encrypt(value)
                else:
                    lead["attrs"][name] = value
            lead["ref"] = present.get(ref_field, lead["ref"])
            touched[key] = lead

        self.conn.executemany("INSERT INTO lead_rejections VALUES (?, ?, ?, ?, ?)", rejections)
        for key, lead in touched.items():
            attrs = json.dumps(lead["attrs"], sort_keys=True)
            pii = json.dumps(lead["pii"], sort_keys=True)
            if lead["lead_id"] is None:
                cur = self.conn.execute(
                    "INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, external_ref, "
                    "attrs, pii_enc, first_file_id, last_file_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (workflow_id, key, lead["last4"], lead["batch_id"], lead["ref"], attrs, pii,
                     file_id, file_id))
                lead["lead_id"] = cur.lastrowid
            else:
                self.conn.execute(
                    "UPDATE leads SET external_ref = ?, attrs = ?, pii_enc = ?, last_file_id = ? "
                    "WHERE lead_id = ?", (lead["ref"], attrs, pii, file_id, lead["lead_id"]))
        self.conn.execute(
            "UPDATE ingested_files SET rows_total = ?, rows_rejected = ?, rows_merged = ?, "
            "field_warnings = ? WHERE file_id = ?", (total, rejected, merged, warnings, file_id))
        summary.lead_rows += total

    # ----------------------------------------------------------------- events
    def _ingest_event_file(self, path: Path, summary: IngestSummary) -> None:
        data = path.read_bytes()
        file_id = self._register_file("events", path, data, summary)
        if file_id is None:
            return
        total = malformed = 0
        rows = []
        for line_no, line in enumerate(data.split(b"\n"), start=1):
            if not line.strip():
                continue
            total += 1
            row = self._parse_event(line, file_id, line_no)
            if row is None:
                malformed += 1
            else:
                rows.append(row)
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, occurred_at_us, "
            "occurred_at_raw, phone_hash, disposition_code, payload, file_id, line_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        duplicates = len(rows) - (self.conn.total_changes - before)
        self.conn.execute(
            "UPDATE ingested_files SET rows_total = ?, malformed = ?, duplicates = ? WHERE file_id = ?",
            (total, malformed, duplicates, file_id))
        summary.events += total

    def _parse_event(self, line: bytes, file_id: int, line_no: int) -> tuple | None:
        """Returns a ledger row, or None if the line is malformed."""
        try:
            ev = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(ev, dict):
            return None
        event_id = ev.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            return None
        ts = parse_timestamp(ev.get("occurred_at"))
        if ts is None:
            return None  # an event we cannot place in time cannot be reported on
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        raw_phone = ev.get("phone")
        if isinstance(raw_phone, int) and not isinstance(raw_phone, bool):
            raw_phone = str(raw_phone)  # some producers send the number as a JSON integer
        phone = normalise_phone(raw_phone) if isinstance(raw_phone, str) else None
        code = payload.get("disposition_code")
        return (event_id.strip(), _str_or_none(ev.get("event_type")), _str_or_none(ev.get("workflow_id")),
                _str_or_none(ev.get("call_id")), to_epoch_us(ts), ev["occurred_at"],
                self.vault.blind_index(phone) if phone else None,
                code if isinstance(code, str) else None,
                json.dumps(payload, sort_keys=True, ensure_ascii=False), file_id, line_no)


def _decode_csv(data: bytes, path: Path, summary: IngestSummary) -> str:
    """UTF-8 (with or without BOM). Files saved by Excel on Windows are often cp1252: fall back
    to that rather than refusing the whole drop, and say so."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("cp1252")
    except UnicodeDecodeError:
        text = data.decode("latin-1")  # never fails; last resort
    summary.notes.append(f"{path}: not valid UTF-8, decoded as Windows-1252/Latin-1")
    return text


def _str_or_none(v):
    return v if isinstance(v, str) and v != "" else None

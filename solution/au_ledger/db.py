"""SQLite schema. See SUBMISSION.md for the reasoning behind each table."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Disposition catalogue, reloaded from dispositions.csv on every run.
CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code        TEXT NOT NULL,
    label       TEXT NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success  INTEGER NOT NULL CHECK (is_success IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
);

-- One row per input file ever ingested. Makes re-ingest a no-op and holds the
-- per-file counters that data_quality.json is summed from.
CREATE TABLE IF NOT EXISTS ingested_files (
    file_id      INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    sha256       TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    workflow_id  TEXT,             -- leads only
    batch_id     TEXT,             -- leads only
    ingested_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    rows_total   INTEGER NOT NULL DEFAULT 0,  -- lead rows (non-blank) or event lines (non-empty)
    rows_rejected INTEGER NOT NULL DEFAULT 0,
    rows_merged  INTEGER NOT NULL DEFAULT 0,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    malformed    INTEGER NOT NULL DEFAULT 0,
    duplicates   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kind, sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_lead_batch ON ingested_files (workflow_id, batch_id)
    WHERE kind = 'leads';

-- Current merged state of each lead. PII is never stored in plaintext:
-- phone_hash is a keyed blind index (for joins), pii_enc holds AES-GCM ciphertexts.
CREATE TABLE IF NOT EXISTS leads (
    lead_id       INTEGER PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    phone_hash    TEXT NOT NULL,
    phone_last4   TEXT NOT NULL CHECK (length(phone_last4) = 4),
    batch_id      TEXT NOT NULL,     -- first valid row's file stem, never changes
    external_ref  TEXT NOT NULL,
    attrs         TEXT NOT NULL,     -- JSON: merged non-PII fields (no defaults applied)
    pii_enc       TEXT NOT NULL,     -- JSON: {field: ciphertext}
    first_file_id INTEGER NOT NULL REFERENCES ingested_files (file_id),
    last_file_id  INTEGER NOT NULL REFERENCES ingested_files (file_id),
    UNIQUE (workflow_id, phone_hash)
);
CREATE TRIGGER IF NOT EXISTS leads_batch_is_permanent
BEFORE UPDATE OF batch_id, workflow_id, phone_hash ON leads
BEGIN SELECT RAISE(ABORT, 'lead identity and batch_id are immutable'); END;

CREATE TABLE IF NOT EXISTS lead_rejections (
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    file_id     INTEGER NOT NULL REFERENCES ingested_files (file_id),
    PRIMARY KEY (workflow_id, batch_id, row_number)
);

-- The interaction ledger: every well-formed, first-seen event, append-only.
-- Linking to leads is *derived* at report time (an orphan today may link tomorrow),
-- so nothing here ever needs to change.
CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,
    event_type       TEXT,
    workflow_id      TEXT,
    call_id          TEXT,
    occurred_at_us   INTEGER NOT NULL,  -- UTC epoch microseconds
    occurred_at_raw  TEXT NOT NULL,     -- as received, for audit
    phone_hash       TEXT,              -- NULL when absent or not a valid mobile
    disposition_code TEXT,
    payload          TEXT NOT NULL,     -- JSON, contains no customer PII
    file_id          INTEGER NOT NULL REFERENCES ingested_files (file_id),
    line_no          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_phone ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call ON events (workflow_id, call_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # we manage transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    return conn


def check_key_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'pii_key_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('pii_key_fingerprint', ?)", (fingerprint,))
    elif row["value"] != fingerprint:
        raise SystemExit(
            "PII key does not match the one this database was created with. "
            "Set AU_PII_KEY / --key-file to the original key.")

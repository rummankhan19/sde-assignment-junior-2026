"""Edge cases found in review: redelivered files, encodings, numeric phones, bad keys, and
a second writer between drops."""
import shutil
import sqlite3

import pytest

from au_ledger.cli import DEFAULT_SCHEMAS, main
from au_ledger.db import check_key_fingerprint, connect
from au_ledger.ingest import Ingestor
from au_ledger.pii import PiiVault
from au_ledger.schema import load_schemas

from .conftest import MOTOR_HEADER, PHONE_A, PHONE_B, Ev, write_events, write_leads

T = "2026-10-01T{}+05:30".format


def test_same_event_file_redelivered_under_new_name_is_counted(ledger):
    d1, d2 = ledger.root / "d1", ledger.root / "d2"
    write_leads(d1, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", PHONE_A, "P_A", "", "", ""]])
    e = Ev()
    write_events(d1, "events_0001", [e.initiated("c1", PHONE_A, T("10:00:00")),
                                     e.connected("c1", PHONE_A, T("10:00:05"))])
    (d2 / "events").mkdir(parents=True)
    shutil.copy(d1 / "events" / "events_0001.jsonl", d2 / "events" / "events_1001.jsonl")
    ledger.ingest(d1, d2)
    ledger.ingest(d1, d2)  # re-running both drops is still a no-op
    rep = ledger.report()
    q = rep.quality
    assert (q["events_total"], q["duplicate_events_dropped"]) == (4, 2)
    assert rep.leads()["P_A"]["total_calls"] == "1"  # duplicates still don't double-count calls


def test_same_lead_content_under_new_batch_name_merges(ledger):
    d1, d2 = ledger.root / "d1", ledger.root / "d2"
    write_leads(d1, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", PHONE_A, "P_A", "", "", ""]])
    (d2 / "leads" / "motor_renewal").mkdir(parents=True)
    shutil.copy(d1 / "leads" / "motor_renewal" / "batch_001.csv",
                d2 / "leads" / "motor_renewal" / "batch_002.csv")
    ledger.ingest(d1, d2)
    rep = ledger.report()
    assert rep.quality["lead_rows_total"] == 2 and rep.quality["duplicate_lead_rows_merged"] == 1
    assert rep.leads()["P_A"]["batch_id"] == "batch_001"


def test_cp1252_lead_file_is_decoded_with_a_warning(ledger, capsys):
    d = ledger.root / "d"
    p = d / "leads" / "motor_renewal" / "batch_001.csv"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"customer_name,phone,policy_number\nJos\xe9 Rao,9876500001,P1\n")
    ledger.ingest(d)
    assert "not valid UTF-8" in capsys.readouterr().err
    vault = PiiVault.load(ledger.root / "pii.key")
    import json
    pii = json.loads(sqlite3.connect(ledger.db_path).execute("SELECT pii_enc FROM leads").fetchone()[0])
    assert vault.decrypt(pii["customer_name"]) == "José Rao"


def test_numeric_json_phone_links(ledger):
    d = ledger.root / "d"
    write_leads(d, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", PHONE_A, "P_A", "", "", ""]])
    write_events(d, "e", ['{"event_id": "e1", "event_type": "call.initiated", "workflow_id": '
                          f'"motor_renewal", "call_id": "c1", "occurred_at": "{T("10:00:00")}", '
                          f'"phone": {PHONE_A}, "payload": {{}}}}',
                          '{"event_id": "e2", "event_type": "call.initiated", "workflow_id": '
                          f'"motor_renewal", "call_id": "c2", "occurred_at": "{T("11:00:00")}", '
                          '"phone": true, "payload": {}}'])
    ledger.ingest(d)
    rep = ledger.report()
    assert rep.leads()["P_A"]["total_calls"] == "1"
    assert rep.quality["orphan_events"] == 1  # a boolean is not a phone


@pytest.mark.parametrize("bad", ["xyz", "ab" * 31, "ab" * 33])
def test_bad_env_key_gives_clear_error(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("AU_PII_KEY", bad)
    with pytest.raises(SystemExit, match="64 hex characters"):
        main(["--db", str(tmp_path / "db"), "report", "--out", str(tmp_path / "out")])


def test_corrupt_key_file_gives_clear_error(tmp_path):
    (tmp_path / "k").write_text("not a key")
    with pytest.raises(SystemExit, match="64 hex characters"):
        main(["--db", str(tmp_path / "db"), "--key-file", str(tmp_path / "k"),
              "report", "--out", str(tmp_path / "out")])


def test_second_writer_between_drops_does_not_break_merge(tmp_path):
    """Writer 1 caches leads during drop 1; writer 2 then adds lead B; writer 1's next drop
    has another row for B. With a stale cache writer 1 would try to INSERT B again."""
    schemas = load_schemas(DEFAULT_SCHEMAS)
    vault = PiiVault.load(tmp_path / "k")

    def writer():
        conn = connect(tmp_path / "db")
        check_key_fingerprint(conn, vault.fingerprint)
        return Ingestor(conn, schemas, vault)

    d1, d2, d3 = (tmp_path / n for n in ("d1", "d2", "d3"))
    write_leads(d1, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", PHONE_A, "P_A", "", "", ""]])
    write_leads(d2, "motor_renewal", "batch_002", MOTOR_HEADER, [["B", PHONE_B, "P_B", "", "", ""]])
    write_leads(d3, "motor_renewal", "batch_003", MOTOR_HEADER, [["B2", PHONE_B, "P_B", "", "", "hi"]])
    w1, w2 = writer(), writer()
    w1.ingest_drops([d1])
    w2.ingest_drops([d2])
    w1.ingest_drops([d3])
    conn = sqlite3.connect(tmp_path / "db")
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2
    assert conn.execute("SELECT batch_id FROM leads WHERE external_ref = 'P_B'").fetchone()[0] == "batch_002"


def test_old_database_layout_is_refused(tmp_path):
    db = tmp_path / "old.db"
    sqlite3.connect(db).execute("CREATE TABLE ingested_files (file_id INTEGER PRIMARY KEY, sha256 TEXT)")
    with pytest.raises(SystemExit, match="older version"):
        connect(db)

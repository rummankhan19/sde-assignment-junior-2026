import sqlite3

import pytest

from .conftest import MOTOR_HEADER, PHONE_A, PHONE_B, PHONE_C, Ev, write_events, write_leads

T = "2026-10-01T{}+05:30".format


def three_leads(drop):
    write_leads(drop, "motor_renewal", "batch_001", MOTOR_HEADER, [
        ["A", PHONE_A, "P_A", "", "", ""], ["B", PHONE_B, "P_B", "", "", ""],
        ["C", PHONE_C, "P_C", "", "", ""]])


# ------------------------------------------------------------ effective disposition
def test_override_beats_set_and_latest_override_wins_by_occurred_at(ledger):
    d = ledger.root / "d"
    three_leads(d)
    e = Ev()
    write_events(d, "e1", [
        e.initiated("c1", PHONE_A, T("10:00:00")), e.connected("c1", PHONE_A, T("10:00:10")),
        e.disp("c1", PHONE_A, T("10:05:00"), "renewed"),
        # later override listed first in the file; occurred_at decides, not file order
        e.override("c1", T("15:00:00"), "not_interested"),
        e.override("c1", T("12:00:00"), "payment_link_sent"),
    ])
    ledger.ingest(d)
    a = ledger.report().leads()["P_A"]
    assert a["current_disposition"] == "not_interested"
    assert a["bucket"] == "completed" and a["ever_success"] == "false"


def test_any_override_beats_a_later_set(ledger):
    d = ledger.root / "d"
    three_leads(d)
    e = Ev()
    write_events(d, "e1", [
        e.initiated("c1", PHONE_A, T("10:00:00")),
        e.override("c1", T("10:01:00"), "renewed"),
        e.disp("c1", PHONE_A, T("10:05:00"), "not_interested"),
    ])
    ledger.ingest(d)
    a = ledger.report().leads()["P_A"]
    assert a["current_disposition"] == "renewed" and a["ever_success"] == "true"


def test_latest_set_used_when_no_override_and_unknown_codes_ignored(ledger):
    d = ledger.root / "d"
    three_leads(d)
    e = Ev()
    write_events(d, "e1", [
        e.initiated("c1", PHONE_A, T("10:00:00")),
        e.disp("c1", PHONE_A, T("10:05:00"), "callback_requested"),
        e.disp("c1", PHONE_A, T("10:06:00"), "renewed"),
        e.disp("c1", PHONE_A, T("10:07:00"), "RENEWD"),             # unknown: ignored
        e.override("c1", T("11:00:00"), "feedback_captured"),       # other workflow's code
    ])
    ledger.ingest(d)
    rep = ledger.report()
    assert rep.leads()["P_A"]["current_disposition"] == "renewed"
    assert rep.quality["unknown_disposition_events"] == 2


# ------------------------------------------------------------ buckets
def test_buckets(ledger):
    d = ledger.root / "d"
    three_leads(d)  # A: telephony-failed only, B: dialed non-terminal, C: no calls
    e = Ev()
    write_events(d, "e1", [
        e.initiated("c1", PHONE_A, T("10:00:00")), e.failed("c1", PHONE_A, T("10:00:02")),
        e.initiated("c2", PHONE_A, T("12:00:00")), e.failed("c2", PHONE_A, T("12:00:02")),
        e.initiated("c3", PHONE_B, T("10:00:00")), e.failed("c3", PHONE_B, T("10:00:02")),
        e.initiated("c4", PHONE_B, T("11:00:00")), e.connected("c4", PHONE_B, T("11:00:05")),
        e.disp("c4", PHONE_B, T("11:05:00"), "callback_requested"),
        # events for a call with no call.initiated do not make a call
        e.connected("c9", PHONE_C, T("11:00:05")), e.disp("c9", PHONE_C, T("11:05:00"), "renewed"),
    ])
    ledger.ingest(d)
    leads = ledger.report().leads()
    assert {k: v["bucket"] for k, v in leads.items()} == {
        "P_A": "in_progress_telephony_failed", "P_B": "in_progress_dialed", "P_C": "yet_to_reach_out"}
    assert leads["P_C"]["total_calls"] == "0" and leads["P_C"]["last_call_at"] == ""
    assert leads["P_B"]["current_disposition"] == "callback_requested"


def test_completed_beats_telephony_failed(ledger):
    d = ledger.root / "d"
    three_leads(d)
    e = Ev()
    write_events(d, "e1", [
        e.initiated("c1", PHONE_A, T("10:00:00")), e.failed("c1", PHONE_A, T("10:00:01")),
        e.disp("c1", PHONE_A, T("10:05:00"), "wrong_number"),
    ])
    ledger.ingest(d)
    assert ledger.report().leads()["P_A"]["bucket"] == "completed"


def test_current_disposition_is_from_most_recent_call_that_has_one(ledger):
    d = ledger.root / "d"
    three_leads(d)
    e = Ev()
    write_events(d, "e1", [
        e.initiated("c1", PHONE_A, T("10:00:00")), e.disp("c1", PHONE_A, T("10:05:00"), "renewed"),
        e.initiated("c2", PHONE_A, T("18:00:00")),  # latest call, no disposition
    ])
    ledger.ingest(d)
    a = ledger.report().leads()["P_A"]
    assert a["current_disposition"] == "renewed" and a["ever_success"] == "true"
    assert a["last_call_at"] == "2026-10-01T18:00:00+05:30"


# ------------------------------------------------------------ daily overview
def test_daily_overview_uses_ist_date_of_initiated(ledger):
    d = ledger.root / "d"
    three_leads(d)
    e = Ev()
    write_events(d, "e1", [
        e.initiated("c1", PHONE_A, "2026-10-01T19:00:00Z"),       # 00:30 IST on Oct 2
        e.connected("c1", PHONE_A, "2026-10-01T19:00:05Z"),
        e.disp("c1", PHONE_A, "2026-10-01T19:05:00Z", "renewed"),
        e.initiated("c2", PHONE_B, "2026-10-02 23:59:59"),         # naive = IST
        e.initiated("c3", PHONE_B, "2026-10-02T09:00:00+05:30"),
        e.connected("c3", PHONE_B, "2026-10-02T09:00:05+05:30"),
    ])
    ledger.ingest(d)
    rows = ledger.report().csv("daily_overview")
    assert rows == [{"workflow_id": "motor_renewal", "date": "2026-10-02", "total_calls": "3",
                     "connected_calls": "2", "connectivity_rate": "0.6667", "success_leads": "1"}]


# ------------------------------------------------------------ event hygiene
def test_malformed_duplicate_orphan_counting(ledger):
    d1, d2 = ledger.root / "d1", ledger.root / "d2"
    three_leads(d1)
    e = Ev()
    good = e.initiated("c1", PHONE_A, T("10:00:00"))
    write_events(d1, "e1", [
        good, good,                                    # duplicate within a file
        '{"event_id": "evt_9999", "event_ty',          # truncated
        '{"event_type": "call.initiated"}',            # no event_id
        "[1, 2]", "", "   ",                          # not an object; empties not counted
        e.initiated("c2", "9123400000", T("10:00:00")),  # never-uploaded number -> orphan
        e.initiated("c3", "12345", T("10:00:00")),       # invalid phone -> orphan
        e.override("c_unknown", T("11:00:00"), "renewed"),  # override for unlinked call
    ])
    write_events(d2, "e2", [good])                     # duplicate across drops
    ledger.ingest(d1, d2)
    q = ledger.report().quality
    assert (q["events_total"], q["malformed_events"], q["duplicate_events_dropped"],
            q["orphan_events"]) == (9, 3, 2, 3)


def test_event_before_lead_links_once_lead_arrives(ledger):
    d1, d2 = ledger.root / "d1", ledger.root / "d2"
    e = Ev()
    write_events(d1, "e1", [e.initiated("c1", PHONE_A, T("10:00:00")),
                            e.disp("c1", PHONE_A, T("10:05:00"), "renewed"),
                            e.override("c1", T("12:00:00"), "not_interested")])
    write_leads(d2, "motor_renewal", "batch_003", MOTOR_HEADER, [["A", PHONE_A, "P_A", "", "", ""]])
    ledger.ingest(d1)
    assert ledger.report("r1").quality["orphan_events"] == 3
    ledger.ingest(d2)
    rep = ledger.report("r2")
    assert rep.quality["orphan_events"] == 0
    assert rep.leads()["P_A"]["current_disposition"] == "not_interested"


# ------------------------------------------------------------ idempotency & storage
def test_reingest_changes_nothing_and_incremental_equals_all_at_once(tmp_path, ledger):
    d1, d2 = ledger.root / "d1", ledger.root / "d2"
    three_leads(d1)
    e = Ev()
    write_events(d1, "e1", [e.initiated("c1", PHONE_A, T("10:00:00"))])
    write_leads(d2, "motor_renewal", "batch_002", MOTOR_HEADER, [["A2", PHONE_A, "P_A", "", "", "ta"]])
    write_events(d2, "e2", [e.initiated("c2", PHONE_B, T("11:00:00"))])
    ledger.ingest(d1)
    ledger.ingest(d2)
    first = ledger.report("a").snapshot()
    ledger.ingest(d1)
    ledger.ingest(d2)
    ledger.ingest(d1, d2)
    assert ledger.report("b").snapshot() == first

    from au_ledger.cli import DEFAULT_SCHEMAS, main
    other = ["--db", str(tmp_path / "other.db"), "--key-file", str(tmp_path / "pii.key"),
             "--schemas", str(DEFAULT_SCHEMAS)]
    assert main([*other, "ingest", str(d1), str(d2)]) == 0
    assert main([*other, "report", "--out", str(tmp_path / "c")]) == 0
    assert {p.name: p.read_bytes() for p in sorted((tmp_path / "c").iterdir())} == first


def test_ledger_is_append_only(ledger):
    d = ledger.root / "d"
    three_leads(d)
    write_events(d, "e1", [Ev().initiated("c1", PHONE_A, T("10:00:00"))])
    ledger.ingest(d)
    conn = sqlite3.connect(ledger.db_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET event_type = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")


def test_no_plaintext_pii_in_database(ledger):
    d = ledger.root / "d"
    write_leads(d, "motor_renewal", "batch_001", MOTOR_HEADER,
                [["Zebulon Quixote", PHONE_A, "P_A", "", "", ""]])
    write_events(d, "e1", [Ev().initiated("c1", f"+91 {PHONE_A}", T("10:00:00"))])
    ledger.ingest(d)
    sqlite3.connect(ledger.db_path).execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = b"".join(p.read_bytes() for p in ledger.root.glob("db.sqlite3*"))
    for needle in (PHONE_A, PHONE_A[:5], "Zebulon", "Quixote"):
        assert needle.encode() not in blob


def test_pii_is_recoverable_with_the_key(ledger):
    import json
    from au_ledger.pii import PiiVault
    d = ledger.root / "d"
    write_leads(d, "motor_renewal", "batch_001", MOTOR_HEADER, [["Asha Rao", PHONE_A, "P_A", "", "", ""]])
    ledger.ingest(d)
    vault = PiiVault.load(ledger.root / "pii.key")
    pii = json.loads(sqlite3.connect(ledger.db_path).execute("SELECT pii_enc FROM leads").fetchone()[0])
    assert vault.decrypt(pii["customer_name"]) == "Asha Rao"
    assert vault.decrypt(pii["phone"]) == "+91" + PHONE_A


def test_wrong_key_is_refused(ledger, tmp_path):
    d = ledger.root / "d"
    three_leads(d)
    ledger.ingest(d)
    from au_ledger.cli import main
    with pytest.raises(SystemExit, match="PII key does not match"):
        main(["--db", str(ledger.db_path), "--key-file", str(tmp_path / "other.key"), "ingest", str(d)])

import pytest

from au_ledger.cli import DEFAULT_SCHEMAS
from au_ledger.schema import load_schemas, validate_row

from .conftest import MOTOR_HEADER, PHONE_A, PHONE_B, write_leads

SCHEMAS = load_schemas(DEFAULT_SCHEMAS)
MOTOR = SCHEMAS["motor_renewal"]


def row(**kw):
    base = {"customer_name": "Asha Rao", "phone": PHONE_A, "policy_number": "P1",
            "policy_expiry": "2026-12-01", "premium_amount": "100", "preferred_language": "en"}
    base.update(kw)
    return base


def test_first_failure_in_schema_order_wins():
    # customer_name comes before phone in the schema
    assert validate_row(MOTOR, row(customer_name="  ", phone="123")).reason == \
        "missing_required:customer_name"
    assert validate_row(MOTOR, row(phone="123", policy_number="")).reason == "invalid_phone"
    assert validate_row(MOTOR, row(phone="", policy_number="")).reason == "missing_required:phone"


def test_invalid_optional_fields_become_null_with_one_warning_each():
    r = validate_row(MOTOR, row(policy_expiry="31/02/2026", premium_amount="N/A",
                                preferred_language="Hindi"))
    assert r.reason is None and r.warnings == 3
    assert r.values["policy_expiry"] is None and r.values["preferred_language"] is None


def test_warnings_not_counted_for_rejected_rows():
    r = validate_row(MOTOR, row(policy_number="", premium_amount="N/A"))
    assert r.reason == "missing_required:policy_number" and r.warnings == 0


def test_empty_optional_is_missing_not_a_warning():
    assert validate_row(MOTOR, row(premium_amount="  ", preferred_language="")).warnings == 0


def test_header_aliases_extra_columns_and_reordering():
    cols = MOTOR.map_headers(["Language", "Insured Name", "Policy_No", "Mobile  No", "agent_notes"])
    assert cols == {0: "preferred_language", 1: "customer_name", 2: "policy_number", 3: "phone"}


def test_defaults_applied_only_to_still_null_fields():
    assert MOTOR.apply_defaults({"preferred_language": None})["preferred_language"] == "en"
    assert MOTOR.apply_defaults({"preferred_language": "hi"})["preferred_language"] == "hi"


def test_merge_rules_end_to_end(ledger):
    d1, d2 = ledger.root / "d1", ledger.root / "d2"
    write_leads(d1, "motor_renewal", "batch_001", MOTOR_HEADER, [
        ["Asha", PHONE_A, "P1", "2026-12-01", "100", ""],
        ["", "", "", "", "", ""],                                   # blank: not counted
        ["Bad", "12345", "P9", "", "", ""],                          # row 2 -> invalid_phone
        ["Asha R", f"+91 {PHONE_A[:5]} {PHONE_A[5:]}", "P1", "", "N/A", "hi"],  # dup in same file
    ])
    write_leads(d2, "motor_renewal", "batch_002",
                ["Mobile No", "Policy No", "Insured Name", "Premium", "junk"], [
                    [f"0{PHONE_A}", "P1", "Asha Rao", "₹2,500", "x"],   # dup across batch
                    [PHONE_B, "P2", "Bo", "", ""],
                ])
    ledger.ingest(d1, d2)
    rep = ledger.report()
    leads = rep.leads()
    assert set(leads) == {"P1", "P2"}
    assert leads["P1"]["batch_id"] == "batch_001"      # first valid row's batch is permanent
    assert leads["P1"]["phone_masked"] == "+91******" + PHONE_A[-4:]
    assert rep.csv("rejections") == [{"workflow_id": "motor_renewal", "batch_id": "batch_001",
                                      "row_number": "2", "reason": "invalid_phone"}]
    q = rep.quality
    assert (q["lead_rows_total"], q["lead_rows_rejected"], q["duplicate_lead_rows_merged"],
            q["field_warnings"]) == (5, 1, 2, 1)

    import json, sqlite3
    conn = sqlite3.connect(ledger.db_path)
    attrs = json.loads(conn.execute("SELECT attrs FROM leads WHERE external_ref='P1'").fetchone()[0])
    # last non-null write wins; nulls (N/A premium, missing expiry) never overwrite
    assert attrs == {"policy_number": "P1", "policy_expiry": "2026-12-01",
                     "premium_amount": "2500.00", "preferred_language": "hi"}


def test_same_batch_with_different_content_is_refused(ledger, capsys):
    d1, d2 = ledger.root / "d1", ledger.root / "d2"
    write_leads(d1, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", PHONE_A, "P1", "", "", ""]])
    write_leads(d2, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", PHONE_B, "P2", "", "", ""]])
    ledger.ingest(d1)
    from au_ledger.cli import main
    rc = main(["--db", str(ledger.db_path), "--key-file", str(ledger.root / "pii.key"),
               "ingest", str(d2)])
    assert rc == 2 and "already ingested" in capsys.readouterr().err
    assert set(ledger.report().leads()) == {"P1"}  # drop 2 rolled back entirely


@pytest.mark.parametrize("workflow", sorted(SCHEMAS))
def test_every_schema_has_one_reference_and_one_identity_phone(workflow):
    s = SCHEMAS[workflow]
    assert s.reference_field and s.phone_field

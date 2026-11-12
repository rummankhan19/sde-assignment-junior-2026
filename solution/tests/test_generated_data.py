"""End-to-end on the provided generator: invariants that must hold for any seed."""
import subprocess
import sys

import pytest

from au_ledger.cli import REPO_ROOT

GENERATOR = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"


@pytest.mark.parametrize("seed", [7, 2026])
def test_generated_drops_incremental_equals_all_at_once(ledger, tmp_path, seed):
    data = tmp_path / "data"
    subprocess.run([sys.executable, str(GENERATOR), "--seed", str(seed), "--scale", "small",
                    "--out", str(data)], check=True, capture_output=True)
    ledger.ingest(data / "drop_1")
    ledger.ingest(data / "drop_2")
    ledger.ingest(data / "drop_1")  # re-run: no-op
    inc = ledger.report("inc")

    from au_ledger.cli import DEFAULT_SCHEMAS, main
    other = ["--db", str(tmp_path / "all.db"), "--key-file", str(tmp_path / "pii.key"),
             "--schemas", str(DEFAULT_SCHEMAS)]
    assert main([*other, "ingest", str(data)]) == 0
    assert main([*other, "report", "--out", str(tmp_path / "all")]) == 0
    assert inc.snapshot() == {p.name: p.read_bytes() for p in sorted((tmp_path / "all").iterdir())}

    q = inc.quality
    assert q["lead_rows_rejected"] == sum(q["rejections_by_reason"].values())
    assert q["malformed_events"] > 0 and q["duplicate_events_dropped"] > 0 and q["orphan_events"] > 0
    batches = inc.csv("batch_summary")
    for b in batches:
        assert int(b["total_leads"]) == sum(int(b[k]) for k in (
            "yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"))
    assert sum(int(b["total_leads"]) for b in batches) == len(inc.csv("lead_report"))
    assert q["lead_rows_total"] - q["lead_rows_rejected"] - q["duplicate_lead_rows_merged"] == \
        len(inc.csv("lead_report"))

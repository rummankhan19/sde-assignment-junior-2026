"""Report generation. All linking and aggregation happens in SQL over the full ledger, so the
reports always reflect everything ingested so far regardless of arrival order. Python only
formats the rows."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

IST_OFFSET_S = 19_800

# Each statement builds one derived temp table. Order matters.
DERIVE = [
    # 1. Phone-bearing events link to the lead with the same (workflow, phone blind index).
    """CREATE TEMP TABLE t_link AS
       SELECT e.event_id, l.lead_id
       FROM events e JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
       WHERE e.event_type IS NOT 'disposition.override'""",
    # 2. Each call belongs to the lead its phone-bearing events link to. (If they somehow
    #    disagree, the lowest lead_id is used so the result is still deterministic.)
    """CREATE TEMP TABLE t_call_lead AS
       SELECT e.workflow_id, e.call_id, MIN(k.lead_id) AS lead_id
       FROM events e JOIN t_link k ON k.event_id = e.event_id
       WHERE e.call_id IS NOT NULL GROUP BY e.workflow_id, e.call_id""",
    # 3. Overrides carry no phone: they link through their call_id.
    """INSERT INTO t_link
       SELECT e.event_id, c.lead_id
       FROM events e JOIN t_call_lead c ON c.workflow_id = e.workflow_id AND c.call_id = e.call_id
       WHERE e.event_type = 'disposition.override'""",
    "CREATE INDEX t_link_ix ON t_link (event_id)",
    # 4. Linked events, minus disposition events whose code isn't in the catalogue.
    """CREATE TEMP TABLE t_valid AS
       SELECT e.*, k.lead_id
       FROM events e JOIN t_link k ON k.event_id = e.event_id
       WHERE e.event_type IS NULL OR e.event_type NOT LIKE 'disposition.%'
          OR EXISTS (SELECT 1 FROM dispositions d
                     WHERE d.workflow_id = e.workflow_id AND d.code = e.disposition_code)""",
    "CREATE INDEX t_valid_ix ON t_valid (workflow_id, call_id)",
    # 5. Effective disposition per call: latest override by occurred_at, else latest set.
    """CREATE TEMP TABLE t_effective AS
       SELECT workflow_id, call_id, disposition_code FROM (
         SELECT workflow_id, call_id, disposition_code,
                ROW_NUMBER() OVER (PARTITION BY workflow_id, call_id
                                   ORDER BY event_type = 'disposition.override' DESC,
                                            occurred_at_us DESC, event_id DESC) AS rn
         FROM t_valid WHERE event_type IN ('disposition.set', 'disposition.override'))
       WHERE rn = 1""",
    # 6. A call exists only if its call.initiated is present and linked.
    """CREATE TEMP TABLE t_calls AS
       SELECT i.workflow_id, i.call_id, i.lead_id, i.initiated_us,
              date(i.initiated_us / 1000000 + {ist}, 'unixepoch') AS call_date,
              EXISTS (SELECT 1 FROM t_valid v WHERE v.workflow_id = i.workflow_id
                      AND v.call_id = i.call_id AND v.event_type = 'call.connected') AS connected,
              EXISTS (SELECT 1 FROM t_valid v WHERE v.workflow_id = i.workflow_id
                      AND v.call_id = i.call_id AND v.event_type = 'call.telephony_failed') AS tel_failed,
              x.disposition_code AS eff_code,
              COALESCE(d.is_terminal, 0) AS is_terminal,
              COALESCE(d.is_success, 0) AS is_success
       FROM (SELECT workflow_id, call_id, lead_id, initiated_us FROM (
               SELECT workflow_id, call_id, lead_id, occurred_at_us AS initiated_us,
                      ROW_NUMBER() OVER (PARTITION BY workflow_id, call_id
                                         ORDER BY occurred_at_us, event_id) AS rn
               FROM t_valid WHERE event_type = 'call.initiated' AND call_id IS NOT NULL)
             WHERE rn = 1) i
       LEFT JOIN t_effective x ON x.workflow_id = i.workflow_id AND x.call_id = i.call_id
       LEFT JOIN dispositions d ON d.workflow_id = i.workflow_id AND d.code = x.disposition_code
    """.format(ist=IST_OFFSET_S),
    "CREATE INDEX t_calls_ix ON t_calls (lead_id)",
    # 7. One row of facts per lead.
    """CREATE TEMP TABLE t_lead_facts AS
       SELECT l.lead_id, l.workflow_id, l.external_ref, l.batch_id, l.phone_last4,
              COUNT(c.call_id) AS total_calls,
              COALESCE(SUM(c.connected), 0) AS connected_calls,
              COALESCE(SUM(c.tel_failed), 0) AS tel_failed_calls,
              COALESCE(MAX(c.is_terminal), 0) AS any_terminal,
              COALESCE(MAX(c.is_success), 0) AS ever_success,
              MAX(c.initiated_us) AS last_call_us,
              (SELECT c2.eff_code FROM t_calls c2
               WHERE c2.lead_id = l.lead_id AND c2.eff_code IS NOT NULL
               ORDER BY c2.initiated_us DESC, c2.call_id DESC LIMIT 1) AS current_disposition
       FROM leads l LEFT JOIN t_calls c ON c.lead_id = l.lead_id
       GROUP BY l.lead_id""",
    """CREATE TEMP TABLE t_lead_bucket AS
       SELECT *, CASE
           WHEN total_calls = 0 THEN 'yet_to_reach_out'
           WHEN any_terminal = 1 THEN 'completed'
           WHEN tel_failed_calls = total_calls THEN 'in_progress_telephony_failed'
           ELSE 'in_progress_dialed' END AS bucket
       FROM t_lead_facts""",
]

BATCH_SUMMARY = """
SELECT workflow_id, batch_id, COUNT(*) AS total_leads,
       SUM(bucket = 'yet_to_reach_out') AS yet_to_reach_out,
       SUM(bucket = 'in_progress_telephony_failed') AS in_progress_telephony_failed,
       SUM(bucket = 'in_progress_dialed') AS in_progress_dialed,
       SUM(bucket = 'completed') AS completed
FROM t_lead_bucket GROUP BY workflow_id, batch_id ORDER BY workflow_id, batch_id"""

DAILY_OVERVIEW = """
SELECT workflow_id, call_date AS date, COUNT(*) AS total_calls, SUM(connected) AS connected_calls,
       COUNT(DISTINCT CASE WHEN is_success = 1 THEN lead_id END) AS success_leads
FROM t_calls GROUP BY workflow_id, call_date ORDER BY workflow_id, call_date"""

LEAD_REPORT = """
SELECT workflow_id, external_ref, batch_id, phone_last4, bucket, total_calls, connected_calls,
       CASE WHEN last_call_us IS NULL THEN NULL ELSE
            strftime('%Y-%m-%dT%H:%M:%S', last_call_us / 1000000 + {ist}, 'unixepoch') || '+05:30'
       END AS last_call_at,
       current_disposition, ever_success
FROM t_lead_bucket ORDER BY workflow_id, external_ref, lead_id""".format(ist=IST_OFFSET_S)

REJECTIONS = """
SELECT workflow_id, batch_id, row_number, reason FROM lead_rejections
ORDER BY workflow_id, batch_id, row_number"""


def _write_csv(path: Path, header: list[str], rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _b(v) -> str:
    return "true" if v else "false"


def _s(v) -> str:
    return "" if v is None else str(v)


def build_reports(conn: sqlite3.Connection, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Read-only snapshot: temp tables live in a transaction we roll back, so a concurrent
    # ingest is never seen half-applied and nothing persistent is touched.
    conn.execute("BEGIN")
    try:
        for stmt in DERIVE:
            conn.execute(stmt)

        _write_csv(out_dir / "batch_summary.csv",
                   ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
                    "in_progress_telephony_failed", "in_progress_dialed", "completed"],
                   (tuple(r) for r in conn.execute(BATCH_SUMMARY)))

        _write_csv(out_dir / "daily_overview.csv",
                   ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate",
                    "success_leads"],
                   ((r["workflow_id"], r["date"], r["total_calls"], r["connected_calls"],
                     f"{r['connected_calls'] / r['total_calls']:.4f}", r["success_leads"])
                    for r in conn.execute(DAILY_OVERVIEW)))

        _write_csv(out_dir / "lead_report.csv",
                   ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
                    "total_calls", "connected_calls", "last_call_at", "current_disposition",
                    "ever_success"],
                   ((r["workflow_id"], r["external_ref"], r["batch_id"], "+91******" + r["phone_last4"],
                     r["bucket"], r["total_calls"], r["connected_calls"], _s(r["last_call_at"]),
                     _s(r["current_disposition"]), _b(r["ever_success"]))
                    for r in conn.execute(LEAD_REPORT)))

        _write_csv(out_dir / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
                   (tuple(r) for r in conn.execute(REJECTIONS)))

        quality = data_quality(conn)
        (out_dir / "data_quality.json").write_text(json.dumps(quality, indent=2) + "\n")
        return quality
    finally:
        conn.execute("ROLLBACK")


def data_quality(conn: sqlite3.Connection) -> dict:
    """Requires the temp tables from DERIVE."""
    f = conn.execute("""
        SELECT COALESCE(SUM(CASE WHEN kind = 'leads' THEN rows_total END), 0) AS lead_rows_total,
               COALESCE(SUM(CASE WHEN kind = 'leads' THEN rows_rejected END), 0) AS lead_rows_rejected,
               COALESCE(SUM(rows_merged), 0) AS merged,
               COALESCE(SUM(field_warnings), 0) AS warnings,
               COALESCE(SUM(CASE WHEN kind = 'events' THEN rows_total END), 0) AS events_total,
               COALESCE(SUM(malformed), 0) AS malformed,
               COALESCE(SUM(duplicates), 0) AS duplicates
        FROM ingested_files""").fetchone()
    reasons = {r["reason"]: r["n"] for r in conn.execute(
        "SELECT reason, COUNT(*) AS n FROM lead_rejections GROUP BY reason ORDER BY reason")}
    orphans = conn.execute(
        "SELECT (SELECT COUNT(*) FROM events) - (SELECT COUNT(*) FROM t_link)").fetchone()[0]
    unknown = conn.execute(
        "SELECT (SELECT COUNT(*) FROM t_link) - (SELECT COUNT(*) FROM t_valid)").fetchone()[0]
    return {
        "lead_rows_total": f["lead_rows_total"],
        "lead_rows_rejected": f["lead_rows_rejected"],
        "rejections_by_reason": reasons,
        "duplicate_lead_rows_merged": f["merged"],
        "field_warnings": f["warnings"],
        "events_total": f["events_total"],
        "malformed_events": f["malformed"],
        "duplicate_events_dropped": f["duplicates"],
        "orphan_events": orphans,
        "unknown_disposition_events": unknown,
    }

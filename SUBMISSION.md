# Submission

## Candidate
- **Name:** _
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** _

## How to run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # also installs ./solution (the `au-ledger` command)

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
au-ledger ingest data/drop_1
au-ledger ingest data/drop_2
au-ledger report --out reports/

pytest solution                           # 89 tests, ~2s
```
- `au-ledger ingest data` (a folder of `drop_*`) or `au-ledger ingest data/drop_1 data/drop_2`
  ingests everything at once, in natural drop order. The result is identical to ingesting the
  drops one at a time.
- State lives in `solution/var/ledger.sqlite3`. The PII master key is `solution/var/pii.key`,
  created on first run and gitignored. Override these with `--db` / `--key-file`, or set
  `AU_PII_KEY=<64 hex chars>`. Start fresh with `rm -rf solution/var`.
- `python -m au_ledger ...` works too, and so does `--help`.

## Design

**Shape:** `ingest` is incremental and transactional. `report` is a full, pure SQL derivation
over everything ingested so far.

```
 lead CSVs ──► header map ─► validate/normalise ─► merge ──► leads (current state, PII encrypted)
                                     └─ rejected ──────────► lead_rejections
 event JSONL ─► parse ─► malformed? count ─► INSERT OR IGNORE ─► events (append-only ledger)
                                               └─ dup? count
 report: events ⋈ leads (blind index) ─► links ─► valid events ─► calls ─► lead facts ─► 5 files
```

The key decision: **linking happens at report time, not at ingest.** The ledger stores every
well-formed, first-seen event, whether or not its lead exists yet. Orphan-ness is derived on
each report by joining against the leads that exist *now*. This means:
- An event that arrives before its lead links automatically once the lead shows up. Nothing is
  re-processed and no row is updated, so the ledger can be truly append-only. Triggers reject
  `UPDATE` and `DELETE` on `events`.
- Drop order (incremental vs all-at-once) can't affect event results. Lead merging is
  order-dependent by spec, and that order is preserved either way.

**Schema (SQLite):**

| Table | Purpose / key constraints |
|---|---|
| `ingested_files` | One row per ingested file, keyed `UNIQUE(kind, sha256)`. Makes re-ingest a no-op and holds the per-file counters (`rows_total`, `rows_rejected`, `rows_merged`, `field_warnings`, `malformed`, `duplicates`) that `data_quality.json` sums. Partial unique index on `(workflow_id, batch_id)` for lead files. |
| `leads` | Current merged state. `UNIQUE(workflow_id, phone_hash)` is the lead identity. `batch_id` is set from the first valid row; a trigger makes it (and the identity columns) immutable. Non-PII fields go in `attrs` (JSON, since schemas differ per workflow); PII goes in `pii_enc` (JSON of ciphertexts). |
| `lead_rejections` | `PK(workflow_id, batch_id, row_number)` plus a reason. No PII. |
| `events` | The ledger. `event_id` PK (dedupe across all drops). `occurred_at_us` is UTC epoch microseconds, so it sorts correctly whatever the source format, and the raw string is kept for audit. Stores `phone_hash`, never the phone. Indexed on `(workflow_id, phone_hash)` and `(workflow_id, call_id)`. |
| `dispositions` | The catalogue, reloaded from CSV each run. |

**PII at rest** (`au_ledger/pii.py`): one 32-byte master key yields three derived keys.
- **Blind index:** HMAC-SHA256 of the canonical phone. Leads and events are joined on this.
  It's keyed on purpose: Indian mobile numbers are a ~4×10⁹ space, so an unkeyed SHA-256 could
  be reversed by brute force in minutes.
- **AES-256-GCM** for the PII values (name, phone). They can still be recovered with the key,
  for example by the dialler.
- **Key fingerprint**, stored in `meta`. Running with the wrong key fails loudly. Otherwise
  every hash would silently stop matching and everything would look like an orphan.

The only plaintext derived from a phone is `phone_last4`, which the masked report needs. A test
greps the raw DB and WAL bytes for names and phone numbers. Event payloads hold no customer PII.

**Transactions:** each drop is a single `BEGIN IMMEDIATE … COMMIT`. A crash or error mid-drop
rolls back completely, and running it again is safe. Reports run inside a transaction that is
rolled back at the end: the derived tables are `TEMP`, and the report reads a consistent
snapshot even if an ingest runs concurrently.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Linking (and therefore orphan and unknown-disposition counts) is derived at report time | Orphans must link once their lead arrives, and counts must reflect the final state. Deriving is simpler and safer than re-linking. | Resolve links at ingest plus a re-link pass when leads arrive. Faster reports, but it mutates the ledger or a side table. |
| File identity = SHA-256 of the content; a seen file is skipped whole | "Re-running ingest changes nothing" includes `events_total` and malformed counts, which per-event dedupe alone would double. | Key on path: breaks if the data directory is moved or renamed. |
| Same `(workflow, batch_id)` with **different** content → error, and the whole drop rolls back | `batch_id` is permanent by spec, so silently mixing two versions of a batch would corrupt reports. Fail loudly and let a human decide. | Treat it as a new batch, or overwrite. |
| A required field that is present but invalid (non-phone) → `invalid:<field>` | The spec only defines `invalid_phone`. Today's schemas have no such field, but the rule had to go somewhere. | Treat it as `missing_required:<field>`. |
| Two columns mapping to the same field → the left-most wins | Deterministic and easy to explain. | Take the first non-empty value per row. |
| An event with an `event_id` but a missing/unparseable `occurred_at` → **malformed** | Without a time the event can't get a call date or an effective-disposition order. Never occurs in generated data. | Store it with a NULL time and exclude it from date-based reports. |
| `events_total` counts lines that are non-empty after stripping whitespace | The spec says "non-empty", and whitespace-only lines carry nothing. | Count raw lines. |
| A `disposition.override` links via `(workflow_id, call_id)` to the lead that the call's phone-bearing events link to. If several leads somehow match, the lowest `lead_id` is used. | This is the spec's rule, made deterministic for an edge case that never happens. | Link the call via `call.initiated` only. |
| Calls are grouped by `(workflow_id, call_id)`. A duplicate `call.initiated` (different `event_id`) → the earliest defines the call time. | Deterministic. | Count each initiated event as a separate call. |
| Effective-disposition ties on `occurred_at` → the higher `event_id` wins | Deterministic. | File order, which isn't stable across drops. |
| `connectivity_rate` uses Python `f"{x:.4f}"` | It matches the spec's example (`0.5625`). It differs from half-up rounding only on exact binary ties at the 5th decimal. | `Decimal` with `ROUND_HALF_UP`. |
| `rejections_by_reason` lists only reasons that occurred, sorted by key | That's what the example shows. Zero-count keys would depend on schema introspection. | List every possible reason with 0. |
| `batch_summary` lists only batches that have at least one lead | A fully-rejected batch has no leads to bucket. | List every ingested batch with zeros. |
| Decimal: strip a currency prefix, then remove **all** commas (needs a leading digit) | Accepts Indian grouping (`1,00,000`) as well as Western. Since this is Indian data, rejecting lakh notation would be wrong. | Strict 3-digit grouping. |
| Dates: 1–2 digit day/month accepted; `Mon` = English 3-letter abbreviation only | Common in uploads and unambiguous since dates are day-first. | Require 2 digits strictly. |
| Defaults are applied when reading (`WorkflowSchema.apply_defaults`), not stored | The spec says "after merging". Storing raw nulls keeps that literal. (It's equivalent either way, because a default is only ever replaced by a later non-null value.) | Store defaulted values. |
| `external_ref` follows last-write-wins like any other field | The spec says later rows overwrite all non-null fields. In generated data the ref never changes. | Freeze it with the first row like `batch_id`. |
| Lead folders whose workflow has no schema are skipped with a warning and **not** recorded as ingested | So they can be ingested later, once a schema exists. | Fail the drop. |
| `last_call_at` is truncated to whole seconds | Every input has whole seconds and the spec example has none. | Keep microseconds when present. |

## Failure modes considered
- **Re-running a drop, or overlapping drops:** whole files are skipped by hash, events are deduped
  by `event_id` across drops, and nothing is double-counted. Tested.
- **Crash mid-ingest:** a single transaction per drop, so the database is untouched and running
  it again is clean.
- **Wrong or lost PII key:** refused via the fingerprint. (A *lost* key means encrypted PII can't
  be recovered and new hashes won't match. In production the key belongs in a KMS, not a file.)
- **Dirty leads:** BOM, renamed or reordered or extra columns, blank rows, short rows (missing
  cells = missing), every phone/date/amount format from the generator, impossible dates, and
  negative amounts.
- **Dirty events:** truncated JSON, non-object JSON, missing `event_id`, invalid UTF-8, invalid
  or never-uploaded phones, overrides for unknown calls, codes from another workflow's catalogue,
  out-of-order events (ordering always uses `occurred_at`, never file order), and events before
  their lead.
- **Concurrent runs:** `BEGIN IMMEDIATE` serialises ingests (with a 30s busy timeout), and
  reports read a snapshot.
- **Not handled:**
  - Corrected re-uploads of a batch (refused by design; this needs a product decision).
  - Deleting or "forgetting" a lead (GDPR/DPDP erasure): the ledger is append-only. The usual
    answer is crypto-shredding with per-lead keys, which I didn't build.
  - Leads keep only their current merged state; the row-level merge history isn't kept.
  - A schema changing between drops isn't versioned.
  - Very large files are read into memory: fine at ~5 MB per file, but not for multi-GB files.

## Testing
- `pytest solution`: **89 tests**, using a temp DB per test with no shared state.
  - `test_normalize.py`: all six phone formats plus nine invalid kinds (including the
    `91`/`0` prefix edge cases), every date format plus impossible dates, currency and rounding,
    enums, header normalisation, and that the three timestamp forms are the same instant, with
    naive = IST across the date boundary.
  - `test_leads.py`: rejection order (first failure in schema order), field warnings (only on
    non-rejected rows), aliases, merge semantics (permanent batch, last non-null wins, blank rows
    not counted, row numbering), and the refused batch clash with rollback.
  - `test_events_and_reports.py`: effective disposition (override beats a *later* set, latest
    override by `occurred_at` not file order, unknown codes ignored), all four buckets plus
    precedence, `current_disposition` vs the latest call, IST day bucketing, exact
    malformed/duplicate/orphan counts, orphan → linked when the lead arrives, re-ingest
    byte-identical, incremental == all-at-once, append-only triggers, no plaintext PII in the DB
    or WAL bytes, PII decryptable, wrong key refused.
  - `test_generated_data.py`: runs the provided generator on two seeds and checks invariants:
    incremental == all-at-once byte-for-byte, and the totals reconcile across all reports.
- **Independent cross-check (not committed):** I wrote a second, in-memory implementation of
  the linking, effective-disposition, bucket and report rules with no SQL. Outputs were
  **byte-identical** on seeds 42, 7, 1, 2026 and 99 (small) and seed 42 (large). It shares the
  normalisation functions, which are unit-tested separately.
- **Mutation check:** I broke six rules by hand and made sure a test fails for each. One mutation
  (dropping override precedence) survived at first, which is how
  `test_any_override_beats_a_later_set` came about.
- **Not tested:** PostgreSQL (SQLite only), concurrent ingest processes, and files bigger than
  memory.

## Results
Timings on a MacBook, SQLite in WAL mode:

| Dataset | ingest drop_1 | ingest drop_2 | report | total |
|---|---|---|---|---|
| small (seed 42: 2,057 lead rows, 8,745 event lines) | 0.09 s | 0.08 s | 0.06 s | < 1 s |
| large (seed 42: 143,990 lead rows, 632,602 event lines) | ~10 s | ~11 s | ~11 s | **~32 s wall** |

Small-seed-42 `data_quality.json`: 108 rejected rows (42 `invalid_phone`, 25/13/14/14
`missing_required:*`), 57 merged duplicates, 122 field warnings, 8 malformed lines, 237
duplicate events, **462 orphans (1,123 after drop_1 alone)**, and 18 unknown dispositions.

Things in the data worth flagging:
- **More than half the orphans after drop_1 are only "early".** 661 events link once batch_003
  arrives. A pipeline that resolved links at ingest time would under-report drop_2's leads.
- **Duplicate events cross drops**, so dedupe has to be global, not per file.
- **Unknown codes are typos.** About 1% of `disposition.set` codes are typos (`RENEWD`,
  `call_back`). Those calls have *no* effective disposition unless a supervisor override fixes
  them, so a "renewed" customer can sit in `in_progress_dialed`. That's worth surfacing to ops.
- **Some rejected rows still get calls.** Leads with no name or ref still receive calls in the
  generator, and those events correctly end up as orphans.
- **Language values like `Hindi`/`english`/`tamil` are warnings**, not the enum codes. They fall
  back to the `en` default, which is arguably the wrong language for `Hindi`. In production I'd
  add language-name aliases to the schema.

**Stretch goals done:**
- **Incremental:** drop 2 never re-reads drop 1, and only new files are processed. Trade-off: the
  report is a full recompute, ~11 s at 140k leads. At much larger scale I'd keep a
  `call_facts` table maintained incrementally per affected `call_id`/lead.
- **Performance:** ~32 s for the large dataset, well under the 2-minute target.
- **SQL aggregation:** all report logic is in SQL (`report.py`); Python only formats output.

## Running this in production (100k leads/day)
- **Triggering:** each drop lands in object storage and triggers an ingest job (Airflow or a
  queue worker), with a single writer per tenant. Content hashes make retries and redeliveries
  free.
- **Database:** Postgres, with `events` partitioned by month. Keep the same append-only rule via
  `REVOKE UPDATE, DELETE` instead of triggers.
- **Report maintenance:** maintain `call_facts` and `lead_facts` incrementally, keyed by the
  calls touched in each ingest, rather than a full recompute.
- **Keys:** the PII key lives in a KMS, with envelope encryption and per-tenant keys. Rotating
  the blind-index key means re-hashing, so it gets its own version column.
- **Backfills and schema changes:** because reports are a pure function of
  (leads, ledger, catalogue), a backfill is just "ingest the late file, regenerate the reports".
  A catalogue change (for example a new success code) retroactively and correctly changes
  historical reports. If clients need frozen reports, snapshot them.
- **Alerting:** alert on orphan-rate and rejection-rate spikes per batch. They are the earliest
  signal of an upstream format change.

## If I had another day
1. Keep the lead merge history (an append-only `lead_rows` table) so "why does this lead have
   value X" can be answered, and `leads` becomes a derivable projection, just like reports.
2. Make report maintenance incremental (`call_facts` keyed by touched calls).
3. Add a Postgres backend behind the same SQL, with CI running the tests on both.
4. Stream event files instead of reading them whole, for multi-GB files.
5. Support crypto-shredding for erasure requests.

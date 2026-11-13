# AI Usage Disclosure

## Tools used
Claude Code (Claude Opus 5.5), running as an agent with shell access on my machine.

## What I used them for
Honestly, the AI did most of the hands-on work, and I directed and checked it. It did these
parts:
- Reading the brief and the data generator with me, and laying out the two assignments.
- Writing the implementation in `solution/au_ledger/`:
  - normalisation rules
  - PII protection (keyed HMAC for matching, AES-GCM encryption)
  - SQLite schema with append-only triggers
  - transactional, idempotent ingestion
  - the SQL that builds the reports
- Writing the 99-test pytest suite.
- Writing a separate, in-memory reference implementation of the report rules (not committed),
  and comparing the two outputs on many seeds.
- Drafting `SUBMISSION.md` and this file.

## What I wrote or decided myself
- I chose Assignment A (Data) over B.
- After the first version was done, I asked for a full review and test pass. When it found
  five edge-case bugs, I decided to fix all five rather than just the counting bug.
- I ran the whole pipeline myself in my own terminal: generating data, ingesting both drops,
  re-ingesting, building the reports and running the tests. I checked the output and asked
  about anything I didn't understand. For example, I asked why a re-run said "skipped
  already-ingested", and learned that it's the idempotency rule working as intended.
- I decided how the work was saved and committed, and that it goes to a private repo.
- I didn't hand-write the code or make the detailed design calls; those came from the AI and
  are listed in `SUBMISSION.md`. Before the interview I'm going through each module so I can
  explain and change it myself, especially:
  - linking events at report time vs ingest time
  - why the phone hash is keyed
  - why a batch clash rolls back the whole drop
  - how the effective disposition is chosen

## Where the AI was wrong or unhelpful
These are real mistakes from this work, caught along the way:
- **SQL NULL bug.** The unknown-disposition filter was first written as
  `event_type NOT LIKE 'disposition.%'`. That evaluates to NULL for events with no
  `event_type`, so they would have been silently counted as unknown dispositions. It was
  fixed to `event_type IS NULL OR …`.
- **A gap in the tests.** The first test suite still passed with override precedence removed
  entirely, because in every test the override came after the `disposition.set`. A
  deliberate mutation check exposed this, and a test was added.
- **A false claim in the write-up.** An early draft of `SUBMISSION.md` said some unknown codes
  came from overrides. Re-reading the generator showed that was wrong, and it was corrected.
- **Five bugs found in the review I asked for.** These were edge cases the generator never
  triggers:
  - The same event file redelivered under a new name was silently skipped, so
    `events_total` undercounted.
  - Non-UTF-8 CSVs crashed the ingest.
  - Phones sent as JSON numbers became orphans.
  - A malformed key gave a raw traceback instead of a clear error.
  - A stale lead cache could crash if a second writer added leads between drops.

  Each was reproduced first, then fixed, with a test that fails on the old code.
- **Shell scripting slip.** A first cross-check script didn't run under zsh and had to be
  rewritten.

## How I verified AI-generated code
- 99 unit and end-to-end tests (`pytest solution`), which I also ran myself: all pass.
- A byte-for-byte comparison against the separate reference implementation on 10 seeds plus
  the large dataset.
- Day-by-day vs all-at-once vs re-run compared byte-for-byte.
- Hand-made mutations of core rules, each confirmed to make a test fail.
- A scan of the raw database bytes for plaintext names and phone numbers: none found.
- A clean install in a fresh virtual environment, using the exact commands in
  `SUBMISSION.md`.
- I ran the full pipeline myself on my MacBook, checked `data_quality.json` and
  `lead_report.csv`, and saw that a re-run correctly skips files already ingested.

# AI Usage Disclosure

> **Draft written by Claude Code, describing its own work. Edit it so it is true for you
> before submitting:** add what you changed or decided yourself, and remove anything you
> can't stand behind.

## Tools used
Claude Code (Claude Opus 5.5), running as an agent with shell access.

## What I used them for
- Reading the brief and the data generator, then choosing Assignment A.
- Writing the whole first implementation in `solution/au_ledger/`:
  - normalisation rules
  - PII vault (HMAC blind index + AES-GCM)
  - SQLite schema with append-only triggers
  - transactional, hash-keyed ingestion
  - SQL report derivation
- Writing the 89-test pytest suite.
- Writing an independent, in-memory reference implementation of the report rules (not
  committed) and diffing the two on six datasets.
- Drafting `SUBMISSION.md`.

## What I wrote or decided myself
_Fill in honestly._ For example: which design decisions you reviewed and agree with, anything
you changed, and which parts you can explain line by line. The interview will probe:
- linking at report time vs ingest time
- why the blind index is keyed
- why a batch clash rolls back the whole drop
- how `t_effective` picks the winning disposition

## Where the AI was wrong or unhelpful
Real mistakes from this session, caught along the way:
- **SQL NULL bug.** The unknown-disposition filter was first written as
  `event_type NOT LIKE 'disposition.%'`. That evaluates to NULL for events with no
  `event_type`, so they would have been silently counted as *unknown dispositions*. Fixed to
  `event_type IS NULL OR …` while re-reading the query before the first run.
- **A gap in the tests.** The first test suite passed even with override precedence removed
  entirely, because in every test the override happened *after* the `disposition.set`. A
  deliberate mutation check exposed this, and `test_any_override_beats_a_later_set` was added.
- **A false claim in the write-up.** The first draft of `SUBMISSION.md` said some unknown codes
  came from overrides. Re-reading the generator showed overrides always use valid codes, so the
  claim was corrected.
- **Shell scripting slip.** The first cross-check script relied on bash word-splitting under
  zsh, so it "failed" without running anything. It was rewritten as an explicit bash script.

## How I verified AI-generated code
- 89 unit and end-to-end tests (`pytest solution`).
- Byte-for-byte diff against an independently written reference implementation on seeds 42,
  7, 1, 2026 and 99 (small) and 42 (large).
- Incremental vs all-at-once vs re-ingest compared byte-for-byte on every seed.
- Hand-made mutations of six core rules, each confirmed to fail a test.
- A raw byte scan of the database and WAL for plaintext names and phone numbers.
- A clean-copy install in a fresh venv, running the exact commands from `SUBMISSION.md`.
- _Add your own: e.g. "read every module line by line", "changed X live to check I understood it"._

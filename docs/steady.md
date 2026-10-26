# Steward's one-shot runner

`gsd steady --once` runs once; Steward owns the schedule (ready by 06:30 and
18:30 Athens) and its 120-minute process-group deadline. gsd's existing systemd
units are unchanged and must stay off. `python3 -m gsd.steady` invokes the same
one-shot command for compatibility, not a scheduler.

- Since is the latest committed pipeline `window_until`. A missing initial cut
  is a configuration failure: install the catch-up store first.
- Until is the earlier of UTC now minus 15 minutes and the minimum `fresh_through`
  for Gmail, WhatsApp and Fathom from `qmd status --json`. The response is validated
  against the vendored `contracts/vendor/qmd.status.v1.json`, using the unchanged
  reference validator in `gsd/contract_validate.py`. Times are epoch seconds.
  Wrong schema, malformed payloads, `alive: false`, missing selected sources or
  null `fresh_through` fail with exit 1 and a reason in the record. Provider
  diagnostics are not forwarded. A source being behind (even rate-limited) is not
  a failure: its completeness bounds the cut, regardless of `last_complete`.
  `bounded_by` names the limiting source, or is null when the 15-minute lag wins;
  `fresh_through` records all three selected sources' values.
  Selection enumerates by arrival (`qmd feed`, X-10), so late-published,
  earlier-dated evidence is selected by the first cut after it arrives.
- Windows up to and including three days use one step. Longer outage windows
  use consecutive two-day pieces (including a shorter final piece).
- Defaults are `--effort-a high --effort-review high --tags`, with the step's
  existing model defaults. `GSD_STEP_ARGV`, if set, is a JSON string-array override.
  `{since}` and `{until}` expand per piece. Managed `--db`, `--since`, `--until`
  and `--run` are appended last; configured bounds cannot override the committed
  cursor. `--dry` and `--restate` do not belong to this runner and are refused.
- An empty time window exits 0 without committing. A selected-source window with
  no retained documents runs no models and commits an empty draft. This advances
  the cut only through the normal atomic commit path, so an empty first outage
  piece cannot strand all later sources. Its `gsd-empty-run:` note is deliberately
  not a carry-over pointer: the last nonempty step's carry-over remains available.
- After the loop, every invocation that acquired the lock runs **one finalisation
  pass**, including `until <= since`, empty steps, failed steps, and failed QMD
  status/cut reads. TERM skips finalisation and stops further sinks.
  In order: upload the local `archive/<step_id>.sqlite` directory, `gsd publish qmd`,
  `gsd backup`, then `gsd publish notion` only if both
  `~/.config/gsd/notion.token` and `notion-check-reviewed` exist.
  Sinks are independent: each is attempted even if an earlier sink fails.
  There are no per-piece publication calls. Quiet invocations heal failed uploads
  and pending projections without replaying any committed window.
- A loop or sink failure exits nonzero; the record's `error` names every failing
  stage, and `published` lists only successful sinks (including `archive`).
  The child's final gsd step error envelope is retained, not provider/model output.
  An already committed cut is not rolled back. A Notion schema refusal or
  incomplete relation pass also fails the runner even if the process exited zero.
  QMD resumes from its publication position; Notion renders and hash-compares all
  records; backup copies all verified `backups/gsd-*.sqlite` snapshots, including
  earlier failed uploads, excluding pre-catchup files and WAL/SHM sidecars.
  No outbox, receipts table or migration is involved.
  Missing archives from older releases use the local-only
  [archive recovery verbs](../README.md#recovering-an-old-missing-draft-archive).

The runner invokes gsd children through its own resolved `bin/gsd`, never PATH.
Its step and model-tool environments put that release's `bin/` first so model
calls to gsd cannot switch releases either. qmd and claude still resolve on PATH.
Promote qmd's `qmd.status.v1` producer (S1) before this consumer.

The runner takes the store directory's `gsd.lock` with nonblocking flock before
reading status; contention fails immediately. It passes that exact descriptor
only to the step child (`GSD_LOCK_FD`), whose live commit validates and duplicates
it rather than deadlocking on a second open. It never takes Steward's heavy lock.

Steps have fresh UUID directories under the store's `steps/`. Existing failed or
killed drafts are never resumed, deleted or mistaken for committed work. SQLite
commits are atomic; carry-over exports for normal steps are written before the
commit. Under `steward.job.v1`, Steward sends TERM to the whole job process group
and allows 60 seconds before KILL. The steady parent traps TERM, stops starting
steps/publications and lets the step child unwind its detached Claude group.
It tallies available artifacts and writes a final record with `terminated: true`,
then exits nonzero within the grace. Even a hard kill cannot make an abandoned
draft advance the cut; SIGKILL cannot emit a final receipt. No blanket cleanup
of old evidence runs is performed.

## Records

Every normally returning, failing or TERM-interrupted invocation emits one
single-line `steward.record.v1` JSON record.
When `STEWARD_RUN_DIR` is set, append only to its `records.jsonl`; otherwise print
the record to stdout. It also atomically saves that identical object, mode 0600,
to `<database directory>/steady-runs/<time_ns>-<uuid>.json` (directory mode 0700).
`gsd intent --json` reads the newest summary for mech-d's process funnel, without
opening Steward's run storage. Failed and TERM-interrupted runs are kept too;
SIGKILL cannot save a summary. Until a summary exists, intent omits the funnel.

Fields: `schema: steward.record.v1`, `provider: anthropic`, `model_calls`,
`terminated` (false normally), `links` (empty unless known), `error` (null on
success), `bounded_by`, per-source `fresh_through`, `kind: gsd-steady`, initial
`since`, planned `until`, `empty_reason`,
`steps` (successful commits, including empty drafts), `counts` (`proposed`,
`accepted`, `amended`, `rejected`, `held`, `records_landed`), `weighted_tokens`,
`minutes` (monotonic elapsed time), and `published` (successful finalisation sinks
in order). An empty piece sets `empty_reason` even if other pieces land changes.
The until field is the planned bound, not a substitute for the committed cursor.

Weighted tokens (`tokens.weighted_tokens`) are summed over
Claude call captures, including Haiku tagging attempts; `model_calls` counts
those calls. The producer's schema is vendored at
`contracts/vendor/steward.record.v1.json`; tests use these same vendored files
and compare them with installed producers' `contract` output when available.
Aggregated manifest totals are not counted again. Failed launches fall back to
complete stream events if no finished usage capture exists. Decision counts use
`status.json`, or the draft's current status if review stopped before exporting it;
landed-record and step counts use commit captures, falling back to the live
change set's version rows if archiving failed after the commit but before the
capture was saved. A failed step process can therefore still report a successful
SQLite commit; its loop error remains set while independent finalisation can
still succeed and be listed in `published`.

`tests/test_steady.py` uses synthetic SQLite, fake clocks and real fake-provider
subprocesses. It includes inherited-lock live-commit plumbing on a synthetic live
path, hard-kill/restart, real parent-and-step process-group TERM cleanup with a
record inside five seconds, empty outage pieces, contract-valid records and publication.

## Outcome ledger (D28)

`gsd runs --json [--since 2026-10-17]` reads only `steady-runs/` and
`steps/steady-*/` beside the selected database path; it does not open the live
store or write back a baseline. `gsd contract gsd.runs.v1` describes this
**gsd-owned** view. Unlinked historical step folders appear as separate rows
with an explicit `no linked steady record` error, not silent omissions.

Each new step's `result.json.outcomes` and steady record's existing `counts`
field add `a_changes`, `r1_accepted`, `r1_amended`, `r1_rejected`, `r1_added`,
`a_held`, `a_undecided`, plus `a_`/`r1_` input/output/cache creation/cache read
and weighted tokens and `cost_usd`. R1 means the complete sequential review
relay, not just agent r1. Decisions count A-origin proposals only; reviewer
additions are separate (unlike legacy accepted/proposed totals). Origin uses
A's id prefix or row provenance, so a misnamed A batch still counts as A.
Provider result usage/cost is preferred; usage captures and streams are fallbacks.
Missing data is null, explained in `missing`; steady counts `measurement_missing`.
Zero means no work, never a substitute for an unreadable draft.

The existing `counts` field also adds `tags_retries`, `tags_malformed_batches`,
`tags_unknown_project_ids`; step `result.json.tag_counts` keeps these even when
both malformed attempts stop preparation. Unknown card ids are dropped, not
retried. No reserved Steward field or vendored Steward contract is changed.

## Worklist contract

`gsd worklist --json` returns `gsd.worklist.v1` from one read transaction:
`at`, the newest landed pipeline `cut` (the same cut as `gsd cursor`), active
projects and open items only. Each declared key is present, null when unknown.
`notion_url` uses `notion_map`; assignment names come from current people, with
no inferred owner. Due/review timestamps are normalized to UTC with an offset;
priority is the stored numeric value rendered as a string.
`priorities.mine` is every open item whose next actor is the user, sorted by
numeric priority descending, due date ascending (undated last), then current
graph sequence. `priorities.waiting_on_others` uses the same order for external
people's items with `at <= due_at <= at + 2 days`; overdue work and agents
are excluded.
Both lists carry id, title, current project name (or null), due_at and priority.
The intent view's "you" counts read this same definition (D29).
States describe progress only; `gsd fields` lists them.
`next_actor` independently names who moves next. `gsd migrate` first takes a
private `VACUUM INTO` backup (`backups/pre-d29-*.sqlite`), then appends ready
versions for current legacy awaiting states, preserving next_actor and history.
The enum trigger is replaced from schema.sql; the migration is idempotent.
`gsd contract gsd.worklist.v1` prints the shipped schema without opening a DB.
`gsd closures --since ISO --json` (`gsd.closures.v1`) returns the same closure rows
as `done` over a caller-chosen window, so a consumer can judge a past day after the
steady run that learned it. `gsd brief ID… --json` (`gsd.brief.v1`) is the per-item
view for preparing the owner's work: summary, next_actions, waiting_on, people,
Notion URL and qmd citations (`tests/test_brief.py`).
`gsd export --json` is unchanged for experiments and is **not a contract**.
`tests/test_worklist.py` validates real CLI output and snapshot consistency
while another WAL connection commits changes.

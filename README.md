# gsd

gsd is a local work graph for a person whose commitments are scattered across mail, chats and meetings. It keeps people, projects, items and meetings in append-only SQLite, then uses a source-citing proposer and review relay to update them. The central distinction is between evidence and work: qmd holds what arrived; gsd reasons about what it means for the work, and lands only reviewed changes through the same validated verbs available to other agents.

## What it offers

- **The work as it stands:** open items, active projects, due dates, assignees,
  next actors and Notion links, with the last committed evidence cut.
- **Your priorities:** open items whose `next_actor` is `user`, ordered by stored
  priority descending, then due date. “Waiting on others” is external people's
  items due in the next two days, not overdue work or agent work.
- **Preparation and outcomes:** an item's summary, next actions, waiting-on text,
  people and source citations; closure events over a caller-chosen window;
  run-level proposal, review, usage and cost measurements.
- **An inspectable history:** row versions, change sets, citations, draft packets,
  decisions and run captures explain what changed and which evidence supported it.
- **A Notion projection:** rendered people, projects, items and meetings, plus a
  root status page. Comments return as evidence for the next pipeline run.
- **Currency without pretending:** the cut is bounded by upstream completeness.
  Missing readings and measurements remain unknown, not zero or healthy.

## How it works

### The graph and its verbs

`schema.sql` defines the base version streams; `gsd migrate` produces the current
schema. `records` identifies entities; `people`, `projects`, `items`, `meetings`,
`record_links` and `source_refs` hold their versions and evidence. Each graph
version has a sequence, stable identity, operation, change-set id, verb, actor
and time. `cur_*` views select the latest version before excluding tombstones.
SQL triggers refuse in-place updates and deletes of graph history.

`gsd verbs --json` advertises arguments, stdin shapes and effects from the same
table used by the parser and dispatcher. `upsert`, `link`, `end-link`, `tombstone`
and batch `write` share validation. `fields` describes the schema; `view` renders
the graph; `show ID…` opens whole records, links and citations. Item `project_id`
and `assigned_to` are authoritative, not parallel ownership links.

### A source-window step

1. **Select:** `gsd step` reads the validated `qmd.feed.v1` arrival feed for Gmail,
   WhatsApp and Fathom, applies selection rules, and captures a private source view.
   Mail must involve the configured owner through addressing or an own-text mention,
   after bulk filtering. Quoted mail history is excluded.
2. **Snapshot:** `snapshot` creates a `VACUUM INTO` draft with `draft_meta`,
   `draft_change` and `draft_ref`, including snapshot sequence maxima.
3. **Propose:** a headless Claude proposer reads the graph and evidence and writes
   changes through gsd verbs. Haiku project tags organize evidence; they are hints,
   not facts. Untagged units stay in the packet.
4. **Review:** fresh reviewers run sequentially over related project/item scopes,
   then meetings, then people. Every proposal needs a decision. Amendments and
   rejections of proposer changes require source quotes; additions are cited too.
5. **Commit:** `commit` replays accepted/amended field deltas, not full draft
   snapshots. Rejected fields cannot leak through later versions. For feed-bearing
   steps, a concurrent-edit refusal rolls back the entire landing, cut and feed
   checkpoint. Deliberate holds/rejections simply do not land.

`gsd steady --once` is the scheduler-facing runner. It starts at the committed
cut and ends at the earlier of now minus 15 minutes and the selected sources'
minimum `fresh_through` from validated `qmd.status.v1`. Windows over three days
are split into two-day pieces. Empty retained input commits without model calls.
Normal steady runs use high proposer/reviewer effort and project tagging.

After stepping, each lock-held invocation attempts archive upload, qmd publication,
backup and gated Notion publication independently—even on a quiet or failed loop.
A failed sink does not undo committed work; later invocations reconcile pending
outputs. TERM stops new work and finalisation, unwinds children and writes a
failure receipt when possible. See [steady](docs/steady.md) and [step](docs/step.md).

### Public read contracts

Each gsd schema is shipped in `contracts/` and printed without a database by
`gsd contract ID`. Every payload identifies its schema; required keys are present,
with null for unknown values where the schema allows it.

| Primitive / command | Contract | Rule |
|---|---|---|
| `worklist --json` | `gsd.worklist.v1` | One read transaction: cut, active projects, open items, ordered priorities and recent closure events. |
| `brief ID… --json` | `gsd.brief.v1` | Per-item next steps, people and qmd citations; unknown/ambiguous ids carry an error. |
| `closures --since ISO --json` | `gsd.closures.v1` | Closure transitions at their graph-version times, including repeated closures, over the requested window. |
| `runs --since ISO --json` | `gsd.runs.v1` | Read existing run artifacts; distinguish missing measurements from zero and reviewer additions from proposer decisions. |
| `intent --json` | mech-d `intent/1` | Counts, projects, flow, freshness chain and own last-run summary; no item titles, people or source text. |

`export --json` is an experimental six-list state dump, **not** a cross-system
contract. Consumers use producer-owned views rather than opening SQLite or parsing
human output. Vendored producer contracts in `contracts/vendor/` validate inputs.
Steady emits `steward.record.v1` receipts under the `steward.job.v1` protocol.

### Projections and comments

`publish qmd` writes change history to qmd's spool as `gsd-events`; selection
excludes that lane to prevent a feedback loop. `backup` creates and verifies a
private SQLite snapshot, then uploads using `rclone copy --immutable --checksum`.
It never copies a live WAL file.

`publish notion --check` plans without remote writes. Ordinary publishing renders
all records, compares hashes, writes changed sections and properties, then sets
relations after page creation. Uncertain writes are read back, not blindly retried.
There is no outbox. Page/property operations request silent notifications; Markdown
updates and workspace automations cannot be promised silent.

`inbox pull` reads signed comment events relayed by the mech-d Worker, with a daily
page-comment sweep as fallback. It refreshes the root status page and stores
comments in a separate `inbox.sqlite`, never directly in the graph. Committed
comment windows can be cleared with `--clear` by recreating eligible leaf blocks;
page-level comments and protected blocks stay. This removes the original block
and its discussion, so enable it deliberately. See [projections](docs/projections.md).

## Design decisions

- **Separate surfaces from reasoning →** the graph stays usable without the
  pipeline → no private agent-only SQL mutation path.
- **Append versions and tombstones →** history retains provenance → no destructive
  edits to graph rows or a second competing change journal.
- **Advertise validated verbs →** agents discover the actual interface → no generic
  arbitrary-diff application or raw SQL write interface.
- **Cite before closing or assigning →** plausible completion is not a decision →
  `done`/`canceled` need a same-operation `decides` excerpt; changed nonempty
  assignees need `commits` or `decides`, not a guessed owner.
- **Leave uncertainty open →** unassigned or unchanged beats invented certainty →
  no pipeline-created owner-question queue or forced escalation.
- **Review deltas before landing →** an accepted later snapshot must not smuggle
  rejected fields → no whole-draft replacement of live records.
- **Enumerate arrivals, report a cut →** late evidence still gets read while currency
  stays explicit → no item-time-window enumeration that silently misses late input.
- **Bound the cut by completeness →** a healthy process is not complete data → no
  advancing beyond a selected source's `fresh_through` or trusting an unknown read.
- **Scope detail to involvement →** work and life belong in one graph, but other
  people's activity need not become personal tasks → summarize uninvolved work
  in project state rather than expanding it into items.
- **Review related work together →** project context comes before meetings and
  people → no fixed number of reviewers or hard per-project singleton rule.
- **Treat Notion as a projection →** one canonical graph avoids sync conflicts →
  no bidirectional property/body merge; comments are the feedback channel.
- **Reconcile outputs independently →** committed work survives remote failure →
  no network transaction around graph landing or new publication outbox.
- **Let Steward schedule →** gsd owns what, not when → no second automatic schedule
  competing with Steward's morning/evening runs.

The rationale and superseded experiments are in [docs/decisions.md](docs/decisions.md).

## The family

- [qmd](https://github.com/aaronkunaseelan/qmd-ak) holds the data and owns source
  access, arrival enumeration and completeness readings.
- [gsd](https://github.com/aaronkunaseelan/gsd-ak) decides **what**: the work graph,
  evidence-backed changes and the read contracts above.
- [Steward](https://github.com/aaronkunaseelan/steward) decides **when**: owner time
  and compute time. It reads `gsd.worklist.v1`, `gsd.brief.v1`, `gsd.closures.v1`
  and `gsd.runs.v1`; it never writes gsd.
- [tg-tools](https://github.com/aaronkunaseelan/tg-tools) delivers over Telegram;
  it is not the work graph or its scheduler.

## Run it

The CLI uses Python 3.11+ and its standard library, including SQLite 3.37+ with
STRICT tables and JSON support. There is no pip runtime dependency. Full automation also
needs Linux/flock, qmd with the shipped contracts, authenticated Claude Code with
the configured models, rclone and its remote, and Steward for scheduling. Notion
needs a configured workspace, integration token and mappings; webhook comments
also need a configured mech-d Worker. These are not provisioned by this repository.

Read [docs/config.md](docs/config.md). Create your own private `owner.json` there
(or set absolute `GSD_OWNER_CONFIG`), using `tests/owner.json` only as a synthetic
shape example. Identity, filters, timezone and backup destination are configuration.
Missing required settings fail when the operation needing them runs.

Install an immutable, tested commit from a checkout, rather than running production
from the working tree. Set `REF` to that commit first:

```sh
RELEASE="$HOME/.local/lib/gsd/releases/$(git rev-parse "$REF")"
mkdir -p "$RELEASE" "$HOME/.local/bin"
git archive "$REF" | tar -x -C "$RELEASE"
chmod -R a-w "$RELEASE"
ln -sfn "$RELEASE" "$HOME/.local/lib/gsd/current.next"
mv -Tf "$HOME/.local/lib/gsd/current.next" "$HOME/.local/lib/gsd/current"
ln -sfn "$HOME/.local/lib/gsd/current/bin/gsd" "$HOME/.local/bin/gsd"
gsd verbs --json
```

Keep mutable data outside the release. `--db FILE` overrides `GSD_DB`, then defaults
to `~/.local/share/gsd/gsd.sqlite`. The implicit default is read-only: graph
mutations require an explicit database path/environment or commit's `--into`.
Use `gsd migrate --db FILE` to initialize a scratch store. Steady requires an
installed initial pipeline cut; this is not a turnkey first-run bootstrap.

`systemd/gsd-inbox.{service,timer}` supplies the 15-minute Notion pass, including
comment clearing. Review paths and clearing policy before installing user units.
`gsd-step.{service,timer}` is a legacy standalone schedule: leave it disabled when
Steward schedules `gsd steady --once`. Its service paths need adaptation to run
from an immutable release. Never swap releases during an active steady run.

Check `gsd worklist --json` for the landed cut and work, `gsd runs --json` for
outcomes/missing evidence, and `gsd inbox status --json` for the comment pass.
`gsd status` describes a **draft's review**, not whole-system health.

## Developing

```sh
nice -n 10 python3 -I -m unittest discover -s tests -q
```

Tests use synthetic stores, owner settings and fake provider subprocesses. Public
archives exclude the private legacy `port/` importer; its dependent tests skip.
`gsd/` holds the CLI, writer, draft/commit, selection, relay and publishers;
`schema.sql` plus migrations define the store; `contracts/` holds public schemas
and vendored inputs; `docs/` explains runner, projections and configuration.

Use scratch databases, never a live migration during development. Single writes
and batch `write` merge supplied fields; `null` clears. Structured JSON columns
are JSON text. A failed batch change rolls back independently of successful
siblings; resend only failed change ids. Successful duplicate ids are errors.
`snapshot` → `write` → `decide` → `status --brief` → `commit --into FILE` is the
manual review path. All changes must be decided, including holds and rejections.
Manual drafts without feed checkpoints retain partial commits with listed refusals.
`view --sparse` bounds narrative text; `show` opens full values. `view --json`
encodes rendered text, not a consumer schema. `step --dry` still calls tagging
models unless `--no-tags` is used; it launches no proposer or reviewer.

### Recovering an old missing draft archive

`archive-check --db FILE --json` lists missing local archives without writing.
`archive-recover STEP_ID --db FILE --json` validates the committed run pointer and
freezes its draft under the store lock, without moving the cut or uploading.
Never reapply the window to recover an archive. A later steady finalisation uploads
it. Live commits freeze mode-600 drafts locally before SQLite commits; remote
upload is outside that transaction. Failed runs remain evidence, not resume points.

For release: test a clean commit, archive it into a read-only release directory,
swap `current` only while idle, install/adapt units, and check contracts, cut and
run outcomes through the installed launcher. Producer-first promotion preserves
old/new contracts side by side for breaking changes; consumers validate reads.

## Licence

MIT. See [LICENSE](LICENSE).

# Source-window steps

## Base re-statement (D13/D14)

On a migrated scratch copy, run the explicit-path scripts in order:
`python3 port/rekey.py FILE`, `python3 port/aliases.py FILE`, then
`python3 port/dedupe_links.py FILE`. Each supports `--dry` (counts, no writes).
QMD reads are capped by `QMD_UNTIL`; each real script uses its own port change set.

`gsd step --restate --dry --since 2026-07-16T00:00:00Z
--until 2026-08-13T10:10:37Z --db FILE --run PRIVATE_DIR` builds one proposer
packet per active project. Without `--dry` it runs those agents sequentially,
then the same dynamic review relay as ordinary steps (links alone supply distance),
then commits. Sequential project writes avoid read/modify/write
races on shared people. The shared prefix has only contract + graph, no selected
window; each proposer gets project record ids and primary-ref seeds and discovers
sources itself within the bounds. Its `open-a-SLUG-ID.md` lists unverifiable prior
facts; the runner combines these into `open.md`. Manifest records mode and agents.
Read-only cache proofs use the manifest's first two agent names instead of `a`/`review`.

D14 retires pipeline owner questions. Old `needs_owner`/`question` payload keys
fail loudly. Uncertain fields stay unassigned or unchanged, explained in `why`;
`hold` does not land and goes to nobody. Direct owner-added question items remain.
Item closure requires a same-op `decides` quote, and a changed nonempty assignee
requires `commits` or `decides`; reviewer amendments use their source quote too.

## Ordinary steps

### Tagged experiment (default)

`--tags` (the default) uses tool-free Haiku 5.5 batches of about 25 units and a
fixed cached system prefix of short start-graph cards. Cards include project state,
open titles, aliases of project people and recently cited chat/thread subjects.
Haiku returns only likely project ids with p ≥ 0.05; omitted ids need no score.
Unknown ids are dropped and counted without a retry. Retained display tags remain
the top two at p ≥ 0.06. Retry/malformed/unknown-id counts are visible in step
results and steady counts, including failed preparations.
Only primary sources dated before the window start supply sticky routes: a chat
or account-local mail thread cited for exactly one project gets p=1 without a call.
Migration timestamps are not source dates. One malformed JSON batch is retried once;
a provider failure or a second malformed batch stops the step.

Units are mail threads, WhatsApp bursts (gap >3h splits, last three previous lines
are context only), and meeting sections/checkbox actions. Attachments belong to
their message. Meeting parents are owned once and citation aliases expand to all
sections. Each section retains its original parent ref/title, so regrouping cannot
leave an action without a usable citation; only that metadata repeats, not content.
`tags.json` records source ownership, refs, top two tags at p≥0.06,
none/people probabilities, cards, model, actual cost and every call's usage/time.
Weighted tokens are `tokens.weighted_tokens`, the unit run records use.

The frozen sparse window groups by top tag in graph order, chronological within
each group, followed by **No project tagged (read in full)**. Every unit appears
once, including media messages whose old sparse view collapsed them; original
short refs and source keys remain unchanged. Tags are explicitly fallible hints.
`--no-tags` retains the pre-experiment source views. All modes share the review plan;
re-statement never tags.

### Dynamic review scopes (D17/D18)

Code assigns each A change exactly once: project/item changes go to the project
with the most touched records; meeting-and-people-only changes go to meetings;
person-only changes go to people. Others go to the largest project bucket.
Ties use sorted project ids. Without a project bucket, fallback seeds the first
project (an unnamed project scope if the graph has none). Item `project_id` is
authoritative; people memberships and citation tags do not route changes.

Start with projects that have changes. Relatedness is shared tagged units plus
twice every current link between projects or their items, regardless of relation
kind. Merge descending positive pairs only when the result has at most 60 changes.
A project is never sharded. Unrelated scopes remain separate even when packed into
one reviewer.

Order scopes by project-group size (largest first), then meetings, then people.
Meetings exists even with no changes when the window has meetings. No other empty
scope runs. Target reviewers = ceil(A changes / 60), at least one for any scope.
Pack whole scopes until reaching total/target, then close that reviewer. This is
a share, not a hard reviewer cap: an indivisible scope can exceed it, and fewer
reviewers can result. Empty meetings shares a reviewer where possible.

Reviewers `r1`, `r2`, … run sequentially with one goals-and-invariants role.
Each packet names its scopes and all earlier/later reviewers by scope. Every
scoped change needs a decision; sources, not plausibility or A's why, judge it.
Corrections use quoted amendments; cited `b-rN-N` omissions are self-accepted.
The meetings slice adds meeting refs and relevant changes as context. People
scope includes earlier person changes, including reviewer additions; earlier
decisions change only through sourced corrections. Review never re-triggers or
re-routes scopes. `groups.json` records scopes, projects and assignments; its final
groups/assignments also include reviewer additions. Dry plans have no A changes:
only an already-known meetings scope can exist, otherwise scopes are empty;
the reviewer preview explains the post-A plan rather than inventing clusters.

After commit, `misroutes.md` lists tagged units without a landed citing change for
that project; counts are in `result.json`. These are grader suspects, not errors.
`manifest.json` also stores A's per-turn usage/thinking-block and character counts,
tool call names/ids and totals. Redacted thinking blocks have no measurable text.


```sh
gsd step --since 2026-08-13T10:10:37Z --until 2026-08-15T10:10:37Z \
  --db /private/scratch.sqlite --run /private/step --dry
gsd export --db /private/scratch.sqlite --json
```

Use a `VACUUM INTO` scratch copy, not a filesystem copy of an active WAL store.
Non-dry steps currently require explicit `--db` (or `GSD_DB`); the implicit live
write default is not enabled. A run directory must be new, is mode 0700, and its
artifacts are mode 0600. Never put one in a repository or shared workspace.

`--extra FILE…` (repeatable) adds labelled written minutes. PDF attachments in
selected mail materialise at four pages; DOCX has no page-range argument because
Azure rejects it. Images are skipped. The manifest records covered pages, newly
spent pages, conservative budget reservations, cached outputs and unavailable
reasons. At approximately 300 pages no further document starts; an unlimited
DOCX can overrun this soft cap by its final document. Fathom share links join the
meeting summaries; account-only links remain links. Six attachment-head lines
are included, and quoted mail is omitted rather than retained as old evidence.

Packets contain the sparse graph, sparse window, people directory, field formats
and previous-step digest. Block order is contract, graph view, sparse window,
role, slice, task. `shared.md` holds the first three blocks, identical throughout
one step; `prompt-NAME.md` holds role/slice/task and acceptance. Headless Claude
Code appends `shared.md` to its system prompt (a cache breakpoint), then receives
`prompt-NAME.md` as its FIRST user message on stdin. Shared text at the start of
one long user message is not enough: a cache hit needs a written breakpoint.
`packet-NAME.md` preserves both parts together for inspection and compaction.
Graph bodies, notes and descriptions are available through `gsd show`, not
duplicated in the sparse index. `--dry` writes `packet-a.md` and `packet-review.md`,
launching no proposer or reviewer. With default `--tags`, Haiku tagging still runs for real. The latter has the final reviewer rules and block structure; its slice
is necessarily a preview until A has proposed changes. Real relay packets are
`packet-r1.md`, `packet-r2.md`, …, with current status, deltas, citations and
touched records inserted in the slice only. `manifest.json` records chars/4
estimates for each view, packet block, shared prefix and per-agent part. These
are estimates, not API token counts: the 13→17 Aug proof estimated ~65k shared
but Haiku's first-call shared cache read was ~124k. Budget an Opus run from its
actual usage too; the ~90k packet target is a chars/4 planning estimate.

Queued, unknown and page-cap attachment outcomes are recorded without blocking
preparation or the parent's commit. Materialised child arrivals are counted
separately, not treated as new messages. A child is excluded only when the same
document was already included under its parent in a committed run; its exclusion
names that commit. Otherwise the new content is selected under the original
parent, retaining its selection reason and commit provenance. The retained
manifest and selected documents supply this evidence; an uncommitted draft is
never proof of inclusion. Without prior selection evidence, the parent must
satisfy the normal source policy. Missing acquisition responses remain unknown.

A and each fresh sequential reviewer use `claude -p`; `--effort-a low|medium|high`
(default medium) affects proposers only; `--effort-review` (default medium) sets
reviewers. Both run with
`--permission-mode bypassPermissions`, `--output-format stream-json --verbose`.
`--a`/`--review` default to `claude-opus-5-5` and must match. Use
`claude-haiku-5-5` for cheap plumbing proofs. User/project/local settings, skills
and MCP configurations are disabled; every launch has the same four built-in
tools (Bash, Read, Write, Edit). Normal CLI authentication/environment still
apply. QMD_UNTIL, GSD_DB and GSD_ACTOR are passed explicitly; cwd is always the
run folder. Neither the source text nor the large prompt travels in argv.
Physical attachments have short `att#…` refs and six head lines, once only;
derived generic URL metadata is omitted because the body carries its link.

There is no HOLD, dispatch, resume or automatic relaunch, and these sessions
are NOT visible in the agent interface. Inspect `agent-NAME.json` (session id, pid,
state) and `captures/NAME.{stream.jsonl,stderr.txt,usage.json,result.json,check.json}`
in the private run folder. Usage is deduplicated by API message id, not one row
per streamed content block; result iterations include thinking tokens when
available. Claude Code also keeps its normal local session transcript. The
runner waits up to one hour and kills its owned process group on return, timeout
or exception, including tool children. A non-success result or the independent
`check.py` failing stops the step; captures and the failed receipt remain. An
existing launch receipt refuses another attempt. Never blindly retry an
indeterminate launch; inspect the draft and session first.

For a read-and-stop cache proof on a dry run, Python callers may use
`step.launch(run, 'a', 'claude-haiku-5-5', until, read_only=True)`, followed by
`'review'`. This prepends a read-only instruction to each real per-agent prompt,
keeps the exact same system/tools, rejects tool use, and records a
`read-only-proof` receipt. It does NOT run or satisfy normal step acceptance.

Reviewer slices show id, why, legacy scores when present, and changed fields only
(`old → new` for updates; nonempty fields for creates), and citation refs with
roles/excerpts, never source bodies. Long values have `[cut]` markers. Registry
mirrors and generated timestamps/attribution are omitted. Record headers use
unambiguous short ids; `gsd show ID…` gives full ids and whole records, and
`gsd status --json` gives uncut reasons. Touched records use the same
sparse renderer as `gsd view --sparse`. Open cited sources FIRST, several ids per
call: `qmd get ID… --no-quoted --attachments head:6`; short refs resolve through
`refs.tsv`, written-minutes `file:` refs through `selected.json`.

Multi-entity field amendments use the existing stdin decision batch; the whole
amendment rolls back if any record or field fails. Records must belong to that
change, and A's quote rule still holds. Single-entity `--set field=value` remains:

```sh
gsd decide --json <<'JSON'
{"decisions":[{"id":"a12","decision":"amend",
 "quote":{"ref":"gmail:EXAMPLE","text":"Direct statement"},
 "set":{"FULL_RECORD_ID_1":{"summary":"corrected"},
        "FULL_RECORD_ID_2":{"next_action":"corrected"}}}]}
JSON
```

Every change must be decided by an agent before commit. A is pinned to actor
`a`; quote requirements follow that provenance as well as the a… id prefix,
so an accidentally misnamed second proposal batch cannot bypass review rules.
For feed-bearing source windows (D30), a refused accepted change rolls back the
whole commit: no records land, and neither the cut nor the arrival-feed position
advances. The step fails rather than reporting `committed`; the next steady run
replays the window against the current graph. Deliberate reviewer holds/rejections
are decisions, not refusals, and do not block other accepted work. Manual drafts
without a source feed retain partial commits, with refusals in `commit.json`.
`changes.jsonl` includes proposal notes, reviewer note/decision/quote/actor,
citations and before/after/field deltas. `state.json` matches the experiments'
six-list current-state export. `open.md`, `notes-GROUP.md`, private QMD/agent
captures, groups, packets, draft and commit receipt remain in the run folder.
The commit's change-set note records this folder; the next step reads only that
explicit predecessor's carry-over, never searches experiment/grading folders.
A missing explicitly recorded previous folder is an error rather than silently
losing its open work. Manual commits without a runner locator do not invent a
carry-over folder.

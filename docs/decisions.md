# Design decisions

These decisions explain the current work graph and the experiments that led to
it. Numbering is retained so references in code resolve. Earlier choices are
qualified where later decisions changed them; an experimental plan is not a
promise of current behavior. The implementation and shipped contracts define
the interface.

## D1. A local SQLite store

Keep the canonical graph in a private SQLite WAL store, outside code releases
and shared source trees. A local transactional store makes snapshots and atomic
landing possible without a database service. This rules out synchronizing the
live database as a file or copying an active WAL database for backup. Backups
use `VACUUM INTO`, verification and immutable/checksummed rclone uploads; the
remote is configuration.

## D2. Advertised verbs are the mutation interface

Expose named, validated commands to agents, generated from the same table as
argument parsing and dispatch. Each mutation carries an atomic change set and
provenance. This permits direct agent interaction without a separate approval
file, but rules out raw SQL and a generic arbitrary-diff write interface.

## D3. Separate surfaces from the pipeline

The graph and read/write verbs hold current work; the pipeline selects evidence,
reasons and calls those same verbs. Separating the two keeps the store useful
without the model runner and rules out a hidden pipeline-only mutation path.

## D4. No separate content-event product

Keep the work tables and their history rather than adding a second stream of
narrated developments and explanations. The useful observable is current work
with evidence, not another generated feed. D5 adds publication of actual change
history, not the deferred narrative content events.

## D5. Append versions, publish change history

Graph tables are append-only version streams. Upsert appends a full version;
delete appends a tombstone. Current-state views choose the newest version before
filtering tombstones. Actor, time, verb and change-set id travel with versions.
This preserves history without a parallel graph journal and rules out in-place
updates/deletes. Auxiliary checkpoints and projection mappings are mutable state,
not append-only graph history.

Publish change sets to qmd's spool as `gsd-events`. Exclude that lane from gsd
selection so the graph cannot treat its own output as fresh independent evidence.

## D6. Keep a small, explicit graph shape

Retain the entity registry, people, projects, items, meetings, links and citations.
Do not restore retired execution, job or sync machinery as graph columns. The
base SQL and runtime migrations are distinct: `migrate` is the DDL entry point,
not a license for application code to guess the installed shape.

## D7. Questions and change sets are first-class

An item may be a task or a directly added question. A change-set row records the
origin, actor, time, step and window of a landing. The pipeline cursor is derived
from committed pipeline windows rather than a second independently updated cut.
D14 removes automatic question generation; it does not remove manually authored
question items.

## D8. Notion is a projection

The SQLite graph is canonical; Notion is rendered from it. Agents change the
graph, not projected pages. This avoids bidirectional merge semantics and makes
a projection rebuild possible. Comments are the input channel, not a second
copy of editable graph fields.

## D9. Deterministic, reconciling publication

A single placement/type table determines each field's property or body section.
Ordinary publication renders all records, compares hashes and writes changed
content, with page creation before relation setting. Read back uncertain writes
rather than blindly retrying them. Confirmed mappings survive later failures.
This rules out a publication outbox and assuming that a timed-out create failed.

Ordinary body updates are section-local. Schema mismatches refuse writes rather
than guessing migrations. A dedicated token file and review marker gate ordinary
remote writes. Page/property operations request silent notifications; Markdown
operations, reminders and workspace automations do not guarantee silence.
The original preservation rule for all comments is amended by D22.

## D10. Attachments remain attached to their evidence

Ask qmd to materialize selected mail attachments, with four-page PDF extraction
and a short head in the pipeline packet; full materialized content is available
through qmd reads. DOCX extraction has no page-range argument. Do not inline
entire documents just to make them discoverable. Shared meeting links use qmd's
attachment/link path; inaccessible account-only links remain links, not imagined
transcripts. Availability and page-budget outcomes remain visible in artifacts.

## D11. One graph for work and life obligations

Use the same projects and items for professional commitments and life areas.
A separate personal graph would fragment the worklist. Scope concerns obligations,
commitments and dates, not collecting intimate detail or other people's private
lives. D25 further narrows when activity deserves item-level detail.

## D12. Project routing is a measured hint

The routing experiment compared a cheap model with a probabilistic classifier
and allowed uncertain evidence to relate to several projects. The reason is to
organize context without converting a fallible routing guess into truth. The
current tagged path uses Haiku; the experimental alternative is not a runtime
requirement. No-project-tagged evidence remains available in full.

## D13. Clean references before testing the pipeline

Restatement starts from a migrated base with primary-source references that
actually resolve. Historical source dates, not migration timestamps, constrain
routing context. This prevents bad references from masquerading as model errors.
Restatement discovers sources per active project and runs proposers sequentially
to avoid shared-person read/modify/write races; it does not advance the arrival
feed. The original experiment's single low-effort arm was superseded by D14–D16.

## D14. Uncertainty is not an escalation queue

Leave unsupported assignment empty and unsupported closure/date changes open or
unchanged, with the reason in the proposal. Holds and rejections do not land and
are routed to nobody. Retired `needs_owner` and proposal `question` keys fail
loudly. This rules out invented owners and pipeline-generated decision queues.

Item `assigned_to` and `project_id` are authoritative: duplicate item ownership
and project links are refused. People aliases retain source evidence; a name
alone is not identity. The original two-day experiment and low/high comparison
were measurement choices, not a permanent source-window size or model policy.

## D15. Reviewer effort is independent of proposer effort

Reviewers used high effort in the comparison so proposer-effort experiments did
not also change review quality. The runner exposes separate proposer and reviewer
effort settings. D16 sets both high for steady operation; raw `step` defaults
remain medium. Experiment settings must not be described as universal defaults.

## D16. High-effort steady reasoning and explicit projection reset

Steady operation uses high effort for proposer and reviewers; tagging also sets
high effort. Confidence/importance scores no longer belong in model instructions,
though the store accepts legacy draft scores. Scores are not a substitute for
evidence and decisions.

A Notion hard cut is a distinct, explicit migration: export the remote schema,
properties, bodies and mappings durably before changing schemas or replacing
whole bodies. The plan is read-only; execution requires explicit confirmation.
This replaces the original legacy-reconcile prerequisite, not ordinary publishing's
section-local behavior. A hard cut may remove old content and detach inline
comments, and must never be mistaken for routine synchronization.

## D17. Dynamic review scopes

Review project/item groups first, meetings next, people last, because the latter
need project context. Assign every proposer change once. Target roughly one
reviewer per 60 changes, keeping whole scopes together; this is not a hard cap
on an indivisible scope. Empty scopes normally do not run, while meeting evidence
can require review without a proposed meeting change. This replaces fixed
three-cluster review and special per-project reviewer rules.

## D18. Relatedness determines grouping; Steward owns the schedule

Related projects are grouped using links between projects/items and shared tagged
source units. Merge related scopes with consideration for size, not arbitrary
labels. Review packets state goals and invariants rather than prescribing a
checklist for every scope. Reviewers are sequential and source-first.

Automatic steady work is scheduled morning and evening by Steward. gsd's legacy
step timer stays disabled in that setup. This rules out competing scheduling
loops and an extra manual diff-approval stage after completed pipeline review.

## D19. Closure still requires the decider's evidence

Past-due work and plausible signs of completion do not justify silent closure.
Transitions to both `done` and `canceled` require a same-operation `decides`
citation with a nonempty excerpt. Changed nonempty assignees similarly require
`commits` or `decides`. Reviewer amendments supply quotes too, not exemptions.
These are code-enforced write constraints; other semantic judgments still need
source-first review. The constraint checks citation structure, not whether a
model's interpretation of the quote is correct.

## D20. Writing as the user is separate from graph reasoning

Recipient-facing writing guidance belongs to the agent skill, not the graph
schema or model of work. It governs writing under the user's name to others,
not every response to the user. That private skill is not shipped here; this
repository does not implement a general tone-matching or message-sending service.

## D21. Manual execution is a scheduling concern

Steward distinguishes manual requests from automatic runs, suppresses ordinary
manual-run completion wakes and spaces automatic successful starts. These are
scheduler policies, not a second gsd scheduler or gsd contract guarantee. The
gsd side exposes one-shot execution and receipts; scheduling behavior must be
checked in Steward rather than inferred from this repository.

## D22. Clear eligible comments only after committed consumption

Ingest comments as evidence before clearing their visible markers. Because the
integration cannot resolve them directly, `inbox pull --clear` recreates an
eligible leaf block then deletes the old one, after a committed cut includes its
comments. A fresh listing leaves late replies for the next run. Uncertain copying
never permits deletion. Page comments, protected headings, blocks with children
and unowned pages remain. This deliberately amends blanket comment preservation;
it is not nondestructive comment resolution.

## D23. Manual run controls belong outside the graph

A tools-menu client can show last/next runs and request execution through Steward.
The platform-specific menu is not part of this repository. The inbox handles
comments, not a Notion-button or run-request polling queue. This keeps execution
requests with the scheduler rather than treating them as work-graph mutations.

## D24. Webhook events cover inline comments

Read signed Notion comment events relayed by the mech-d Worker and fetch the
comment through Notion. A daily page-comment sweep is fallback, not proof that
inline comments were exhaustively polled. Events carry ids through the relay;
comment text stays in gsd's private inbox. This rules out assuming that a comment
changes page edit time or that page-level listing discovers every line comment.

## D25. Detail follows involvement

Create detailed outcomes and tasks for work involving the configured owner:
requests, commitments, decisions, attendance or responsibility for a result.
Other people's uninvolved activity belongs in project `current_state`, not an
expanded personal task list. Unclear existing work stays rather than being
silently discarded. This is a reasoning instruction, not a mechanical proof
that every generated item meets the policy.

## D26. Alias delivery does not establish involvement

After bulk filtering, mail must have an owner address in From/To/Cc or mention
an owner name/address in its subject or own text. Configured sender keep rules
override bulk drops, not this involvement check. Quoted history cannot supply
the mention. This general rule avoids maintaining a special list of group aliases.
Names, addresses and filtering lists come from private owner configuration.

## D27. Enumerate by arrival, report currency by cut

Consume `qmd.feed.v1` from durable per-source positions instead of enumerating
item-time windows. Late-published old evidence must still be considered. Source
reads remain as-of bounded, and enumeration stops before invisible arrivals.
Commit checkpoints with the graph and cut, including filtered-only commits.
Failed, dry and restated runs do not advance them. Bootstrap from the installed
cut rather than replaying the whole feed epoch.

## D28. Evidence and measurement, not context expansion by default

OCR text exposed by qmd is citable evidence; a raw unread image is not text
evidence. Tagging asks only for likely projects, drops/counts unknown ids and
retries malformed JSON once rather than retrying unknown project guesses.
A proposed larger proposer-context trial was withdrawn; it is not an enabled
feature. Outcome measurements remain: distinguish A-origin decisions from
reviewer additions, record usage/cost, and represent missing evidence as null
with a reason. A run ledger does not manufacture a successful baseline.

## D29. Progress state and next actor are independent

Use inbox, ready, running, blocked, done and canceled for progress. `next_actor`
says user, agent, external or none; legacy awaiting states migrate to ready while
preserving next actor and history. This avoids two competing definitions of
waiting. The producer builds the ordered owner and near-term external priority
lists; consumers render them rather than re-deriving the selection or order.

## D30. A refused source-window change rolls back the landing

A concurrent live edit must not cause an accepted change to disappear while the
pipeline advances past its evidence. For feed-bearing windows, any commit
refusal rolls back records, cut and feed positions; the next run replays against
the current graph. Deliberate reviewer holds/rejections remain decisions, not
refusals. Manual drafts without a source feed keep partial-commit semantics.

## Cross-system contracts

The producer owns specific views; generic consumers own generic input formats.
Ship schemas with releases, print them through `contract`, include schema ids,
and validate at the reader. An invalid reading is unknown with a reason, never
an empty healthy list. Additive keys keep a version; renamed, removed or
redefined fields require a new version, promoted producer-first with overlap.

qmd owns data, gsd owns what, and Steward owns when. Steward reads
`gsd.worklist.v1`, `gsd.brief.v1`, `gsd.closures.v1` and `gsd.runs.v1`, never gsd's
store or experimental export, and never writes gsd. Run receipts and projection
outputs name their readers and origins. These boundaries prevent two systems
from independently defining the same meaning.

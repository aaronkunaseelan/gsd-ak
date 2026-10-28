# Projections build status

## D16 hard cut

`gsd publish notion --hardcut --plan --db FILE` reads the four configured data
sources and emits JSON schema-operation and page-action counts, with ids but no
page content. It requires a token; it never writes to Notion or changes mappings.

`gsd publish notion --hardcut --yes --db FILE` makes those sources match `FIELDS`
(names and first-listed types) and the current graph. It requires the existing
mode-600 token file, but **not** the old review marker. Both modes hold the ordinary
per-store publish lock. Migrate and seed the intended store separately first.

Before any remote or mapping write, the cut saves a complete, fsynced undo export
under `~/.local/share/gsd/notion-export/<UTC timestamp>/`: original data-source
schemas, page properties (including complete paginated property responses), page
bodies as `.md`, and `notion_map.json`. Directories are mode 700, files 600;
`complete.json` marks a completed export. Missing/incomplete reads or export errors
stop the cut before writes. Already-trashed pages are outside the active inventory.
An inaccessible mapped page absent from that inventory is recreated, not exported.

Each changed schema gets one PATCH. The real title is renamed to Name, never
removed; other legacy fields are deleted/added rather than guessed as aliases.
Relations use single-sided links to the correct data source. Reachable mapped
pages get canonical properties and whole-body replacement; missing, trashed,
misplaced, duplicate-mapped or unmapped identities are created. Relations follow
in pass two. Pages left without an active mapping are trashed, including pages
formerly shielded by stale mappings for deleted records. A second plan compares
remote values and bodies, not just stored hashes, and reports no work after a cut.

**D16 replaces the original reconcile/comment-probe prerequisite below.** Whole-body
replacement explicitly permits deleting old content, including child content; it
may detach inline comments on replaced blocks. No comment API writes are made.
Ordinary publishes remain section-local, use the existing hashes, and still
require the review marker. A successful cut writes that marker with a D16 note.
Uncertain schema/body/archive writes are read back rather than blindly retried.
No live API call or live-store write was made during this build.

## Original W4/W4b commands

- `gsd migrate --db FILE` adds schema v2: `notion_map`, `notion_sources`,
  `publish_positions`. No live migration was run during the build.
- `gsd publish notion --seed --db FILE` imports the legacy page/data-source maps
  without overwriting existing mappings. Run separately from `--check`.
- `gsd publish notion --check --db FILE` renders every current record and reports
  create/update/unchanged counts, schema differences and source configuration.
  Without a token it explicitly uses the expected schema in `gsd/notion.py: FIELDS`;
  this is not a remote reconcile. With credentials it reads each data-source schema,
  mapped page edit times (since the configured import cutoff), canonical property
  values, missing/duplicate section markers and the data-source inventory. Body
  bytes are not compared: Notion normalizes Markdown. Checks hold the publish lock
  and append bounded counts/ids (never values or bodies) to the existing
  `notion-publish.jsonl` ledger. The inbox's existing telemetry/1 output carries
  the latest check's counts, sample ids and observation time; no check or a failed
  check is unknown/red, as is an observation older than
  `notion.PROJECTION_CHECK_MAX_AGE`. The morning steady checks after publishing,
  once per local day (the publication ledger owns the last observation), and
  records the added time as `counts.notion_check_seconds`. Steward schedules
  steady; each publish/check child takes the existing Notion lock. Public REST does not expose
  database automation rules: the owner must inspect these manually.
- `gsd publish notion --db FILE` now has a working coordinator. Remote writes require
  **both** `~/.config/gsd/notion.token` (nonempty, mode 600) and
  `~/.config/gsd/notion-check-reviewed`. An environment token alone cannot open this
  gate. A per-store `.notion.lock` prevents concurrent timer/manual publishers.
- `gsd publish notion --probe-comments PAGE_ID` is an explicitly mutating probe on
  one owner-named page, with the same token/review gate. See first-live-session steps.
- `gsd publish qmd --db FILE [--spool DIR]` emits each change_sets row once, ordered
  by SQLite insertion rowid. Durable payload + index fsync precede advancing the
  database position. A crash can replay an entry; QMD native ids deduplicate.
  Existing port history without a change_sets row is not invented/backfilled.
- `gsd backup` reads live through mode=ro, VACUUMs to a private timestamped file,
  verifies integrity, table counts and sequence maxima, then copies the whole
  backups directory with `rclone copy --immutable --checksum --include 'gsd-*.sqlite'`.
  This heals earlier failed uploads and excludes `gsd.pre-catchup-*` files and
  WAL/SHM sidecars. Concurrent source movement fails closed; rerun rather than
  copying the live file. No copyto.

## Notion renderer and write contract

`FIELDS` is the single placement/type table: each public gsd field occurs once,
with either a property name or body section. Ingest/sync plumbing is not rendered.
The July body design and July 22 action amendment define the layout:
Summary → compiled fields → Kind Notes → generated linked items (and meeting Tasks
by Linked Project) → Sources → gsd id → optional Metadata. Meeting attendees are
names/emails, not JSON. Meeting action columns render within Meeting Notes, not
separate action properties. Exact meeting timing/logistics are property-only;
When is the starting calendar date. Project/item Next Actions are property-only,
with em-dash bullets for multiple lines. Notes preserve canonical markdown except
H1/H2 headings are demoted outside code fences in every generated section, using
`notion.markdown_lines`/`markdown`. H1/H2 headings delimit sections; internal
dividers are content, not Notes-specific boundaries. No managed child database is rebuilt.

At runtime the data-source schema chooses the REST encoder: title, rich_text,
select/status, multi_select, date, number, url, email, checkbox or relation. People
properties are not used. Only types allowed by the placement table are writable.
For ordinary publishes, missing/mistyped properties are reported; a schema/source
discrepancy refuses the entire publish before any remote writes. Only the explicit
D16 hardcut migrates properties; ordinary publishes never guess a migration.
Extra properties (including computed rollups) are reported, not mutated. Until the
first token-backed check, the expected property names/types remain unverified
against the current legacy schemas. Life-area records use the configured sources
by kind, like other records; no separate databases or implicit routing are created.

Each run renders all records, hashes values/body/live property types, then plans.
Pass 1 creates missing identities **without relations**, updates non-relation
properties, and edits body sections. Pass 2 encodes relation record IDs as confirmed
page IDs. An attempted create saves a null page identity before its POST; uncertain outcomes
remain in `notion_map` for discovery on later publishes, never another automatic
create. `db.migrate` upgrades the auxiliary mapping to permit this pending identity.
A confirmed create saves its page identity immediately with null hashes; final
hashes advance per page only after both passes succeed. A later failure therefore
leaves earlier confirmed pages durable and incomplete pages eligible for completion.
There is no outbox. An unchanged hash performs no page writes.

Existing bodies are read before editing. Each changed section uses only:

```
PATCH /v1/pages/{id}/markdown
{"type":"update_content","update_content":{"content_updates":[{"old_str":"…","new_str":"…"}]}}
```

Headings are section markers; duplicate markers or incomplete markdown fail closed.
An interior divider in a section whose content differs from its desired canonical
rendering is ambiguous: it might hide an unheaded owner block or a missing marker.
The entire page body stays unchanged and its completion hashes stay null; page
errors and `--check`'s `ambiguous_sections` findings name the sections. Exact
canonical sections with legitimate internal dividers round-trip unchanged.
Missing sections are appended using `insert_content` at the end and listed in
`appended_sections` (also predicted by a token-backed check). Unrelated headings,
blocks and managed child databases are not replaced. Emptying previously rendered
Metadata clears it in place to `_(empty)_`, retaining its wrapper rather than
removing potentially commented blocks. Ordinary publishes send no whole-page
replace, block deletion, comment write/delete, or `allow_deleting_content`.

Requests are spaced at least .34 seconds (~3/s). HTTP 429 honours `Retry-After`
(seconds or HTTP date), with bounded retries. A 5xx/408, transport timeout or broken
response on a write causes read-back, **not a write retry**. Property writes compare
normalized values; section writes/appends compare the current markdown. Uncertain
creates query the data source (paginated) and locate the stable gsd-id body section,
then compare page properties. Every missing-page create also queries first, so a
crash between creation and mapping does not cause an immediate duplicate. If
read-back cannot confirm success, stop for reconciliation; do not assume that an
empty query proves a timed-out create will never land. Never automatically repeat
that create. Do not print credentials or server error bodies.

Ordinary create discovery queries each source once per publish and inspects only
unmapped page IDs; claimed IDs leave that in-memory list. An uncertain create uses
a fresh query, still excluding mapped pages, with duplicate-ID refusal and full
property read-back retained. Markdown update/append warnings record only
`notion warning: <code>` in the page's errors, prevent its completed hashes from
advancing, and let other pages finish. Publish reports include `requests` (method
and route-class counts including retries, HTTP 429/5xx, Retry-After and pacing
seconds, and elapsed seconds). Each ordinary non-check publish also appends a
mode-600 `<store dir>/notion-publish.jsonl` line containing only a timestamp,
request counters, plan totals and published count—never IDs, URLs or content.
Hard-cut behavior and the steady/Steward record contracts are unchanged.

### Notifications: available suppression is used, not overstated

Create-page and update-page support `notifications: {"mode":"silent"}`. All page
creates and property/relation updates send it. The Markdown endpoint documents no
notification-suppression parameter; none is invented. Even page silent mode does
**not** suppress automation notifications, reminders or webhooks. The check/publish
reports explicitly warn that complete silence is not guaranteed. Review database
automations before enabling writes.

Official docs consulted 2026-10-16 (Markdown API version `2026-03-11`):
- https://developers.notion.com/reference/post-page.md (notifications and markdown)
- https://developers.notion.com/reference/patch-page.md (notifications and properties)
- https://developers.notion.com/reference/update-page-markdown.md (section updates,
  append, protections and endpoint schema; no notifications option)
- https://developers.notion.com/reference/retrieve-a-data-source

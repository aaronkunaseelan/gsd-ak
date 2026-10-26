# Configuration

Stdlib only. Private files are not part of this repository. Missing owner keys
raise an error naming the key when the operation needing it runs.

## Owner settings

`~/.config/gsd/owner.json`, overridden by `GSD_OWNER_CONFIG` (absolute path),
is a JSON object. Settings are cached per path for the process lifetime.
`tests/owner.json` is a complete synthetic example; the discovery suite selects
it explicitly and passes its path to child processes.

| Key | Meaning | Example |
|---|---|---|
| `email` | Primary owner address | `owner@example.test` |
| `personal_email` | Also matches +tag variants | `pat@example.test` |
| `name` | Full name; words match mentions, first word labels chats | `Pat Synthetic` |
| `organisation` | Organisation named in the agent prompt | `Example Organisation` |
| `whatsapp_jid` | Owner JID, including device suffix matching | `12025550101@s.whatsapp.net` |
| `whatsapp_aliases` | Exact QMD sender aliases | `["owner"]` |
| `keep_from` | Sender suffixes/addresses overriding bulk drops | `["@organisation.test"]` |
| `bulk_domains` | Domains; trailing dot matches a domain fragment | `["digest.test", "retailer."]` |
| `bulk_local` | Local-part regex, matched from the start | `"^(no-?reply)(\\+.*)?$"` |
| `backup_remote` | rclone base for snapshots; archives add `steps/` | `test_remote:example-backups/gsd/` |
| `timezone` | IANA owner prose/flow timezone | `Europe/Helsinki` |
| `runs_since` | Default `runs --since` date | `2026-01-01` |
| `flow_since` | First date of the intent flow | `2026-01-01` |
| `notion_user_id` | The owner's Notion user id; the step's comment view marks that author `owner` | `00000000-0000-4000-8000-00000000a0a0` |
| `notion_import_cutoff` | Ignore later edits when reading legacy pages | `2026-01-01T00:00:00Z` |

## Other files and environment

- `~/.config/gsd/notion.token`: Notion API token; `NOTION_API_TOKEN` is the
  fallback. `~/.config/gsd/notion-check-reviewed` is the owner review flag.
  Notion workspace/root ids and renderer mappings are local state
  under `~/.local/share/gsd/`, created by the Notion commands.
- `~/.config/gsd/steady.env`: systemd environment file, optional. Common keys:
  `GSD_OWNER_CONFIG`, `GSD_STEP_ARGV` (JSON argv array for steady's step),
  `NOTION_API_TOKEN`. Never commit credentials.
- `~/.config/mech-d/collector.env`: `MECH_D_URL` (e.g. `https://collector.example.test`)
  and `MECH_D_TOKEN` (private bearer token) for the mech-d Worker API.
- `GSD_DB`: database path override; `--db` takes precedence. Default:
  `~/.local/share/gsd/gsd.sqlite`. `GSD_ACTOR`: write attribution, default `owner`.
- `QMD2_HOME`: qmd publish destination, default `~/.local/share/qmd2`.
- Runner context: `QMD_UNTIL` caps source reads; `GSD_LOCK_FD` inherits the store
  lock; `STEWARD_RUN_ID`/`STEWARD_RUN_DIR` link the scheduler's run receipt.
  The runner sets `CLAUDE_CODE_MAX_OUTPUT_TOKENS` for tags and
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY` for model subprocesses.
  `PATH` resolves `qmd`, `steward`, `claude`, `rclone`; `HOME` resolves local paths.

Services assume immutable releases under `~/.local/lib/gsd/current` and a source
checkout under `~/tools/gsd`; adapt unit paths for another installation.
Timers use host-local time. Disable optional timers if Steward owns scheduling.
The legacy `port/` importer is private and excluded from public archives.

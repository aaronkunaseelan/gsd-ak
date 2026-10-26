# gsd: instructions for agents and contributors

Read [README.md](README.md) for what gsd does and [docs/decisions.md](docs/decisions.md)
for why. D32 governs where anything you add or change is written.

## One owner per fact (D32)

Every value, vocabulary, default, rule and payload shape has exactly one owner.
Change the owner; everything else derives from it, prints it or points to it.

| Kind of fact | Owner | Exposed by |
|---|---|---|
| Table column, vocabulary, required field | `schema.sql` plus `db.migrate` | `gsd fields` |
| Field default | `write.DEFAULTS` | `gsd fields` |
| Verb, its flags, flag defaults, effect | `cli.VERBS` | `gsd verbs --json`, `gsd VERB --help` |
| Judgement principle for agents writing the graph | `packets.PRINCIPLES` | `gsd rules`, the pipeline contract, the reviewer role |
| Payload a consumer reads | `contracts/<id>.json` | `gsd contract ID` |
| Payload gsd reads from another tool | the producer; `contracts/vendor/` is a byte-for-byte copy of its `contract` output | the producer's `contract` |
| Threshold, window, cap, pacing | a named constant in the module that uses it | its name, cited in docs |
| Notion placement of a field | `notion.FIELDS` | `gsd publish notion --check` |
| Owner or private setting | a key in `owner.json`, read by `owner.get` | `docs/config.md` (meaning, not value) |
| Weighted-token unit | `tokens.weighted_tokens` | run records |
| Another system's fact (Steward's schedule, qmd's sources) | that system | its own CLI; link to it |
| Why something is the way it is | a D-entry in `docs/decisions.md` | — |

## Prose

- README, `docs/` and agent skills that wrap gsd explain behaviour and reasons.
  They name the owner (a command, a constant, a table) instead of copying its
  value, vocabulary, default or rule text. Where a reader needs the value, show
  the command that prints it.
- A copy is allowed only where its reader cannot call the owner: prompt text
  sent to pipeline agents, or a vendored contract. Every such copy is either
  generated from the owner or checked against it by a test.
- `docs/decisions.md` is append-only history. Do not edit an old entry to
  describe current behaviour; add a new D-entry that qualifies it. A new entry
  ends with `Lives in:` naming the owner.

## Making a change

1. Change the owner only. If you find yourself writing the same value in a
   second place, first make one derive from the other.
2. Anything agents act on (verbs, fields, rules, contracts) is printed by the
   CLI. Skills and docs point to that command rather than restating it.
3. A new or changed judgement principle is one `packets.PRINCIPLES` entry
   plus a D-entry. Nothing else is edited by hand.
4. Contracts: additive keys keep the version; anything else is a new version,
   promoted producer-first (decisions, "Cross-system contracts"). Re-vendor
   another tool's contract verbatim from its `contract` output, in its own commit.
5. Run the whole suite before committing:
   `nice -n 10 python3 -I -m unittest discover -s tests -q`.
   `tests/test_single_source.py` and `tests/test_contracts.py` fail when a
   copy drifts from its owner. Fix the copy; never loosen the check.

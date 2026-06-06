# cdx-care

`cdx-care` is a local operator CLI for Codex app/CLI support-root state under
`~/.codex`. It is designed for repeatable, evidence-backed diagnosis and
guarded reconciliation of DB/state drift without editing vendor Codex source.

## Safety contract

- `doctor`, `plan`, `diagnose`, and `raw sql --readonly` are read-only for
  Codex DBs.
- `apply` and `run --apply-approved` deny DB writes when Codex has live handles
  open on the target DBs.
- Every row-level DB write is planned first with exact keys, schema fingerprint,
  old-value preconditions, backup of DB/WAL/SHM, transaction, readback, and a
  receipt.
- Plan files are untrusted input at apply time: action DB paths, lanes, tables,
  schema table sets, update/insert columns, git repo, git paths, and `run_id`
  are revalidated against the closed v1 policy.
- Raw SQL is read-only only. There is no raw write escape hatch.
- Raw SQL returns raw selected values; do not query private text/body columns
  unless you intend to view them locally.
- User-supplied plan files and diagnostic packs are created with private modes
  and refuse to overwrite existing paths. `diagnose --out-dir` must name a new
  directory; existing directories are not chmodded or reused.
- Managed `~/.codex/cdx-care/**` artifact directories for generated run plans,
  backups, and receipts are forced to `0700`; plan, backup, and receipt files
  are created `0600`.
- Session repair writes only the legacy `session_index.jsonl` name index after
  state/rollout-file alignment proof. It replaces distinct `state_5.sqlite`
  titles and preserves current latest-entry fallback names for threads whose
  SQLite title is still blank/default. `history.jsonl` is append-only message
  history and remains diagnostic-only.
- Memory git hygiene runs the admitted `.DS_Store` commit with Git hooks and
  global/system config disabled; the lane must not execute ambient repo hooks.
- Log DB compaction is a guarded standalone `VACUUM` lane after DB-family
  backup, disk-space preflight, integrity/schema checks, and row-count readback.

## Commands

```bash
cdx-care --json doctor
cdx-care --json prep --profile workstation
cdx-care --json apply --plan ~/.codex/cdx-care/plans/<run_id>.json
cdx-care --json plan --profile workstation --out /tmp/cdx-care-plan.json
cdx-care --json apply --plan /tmp/cdx-care-plan.json
cdx-care --json run --profile workstation --apply-approved
cdx-care --json prep --profile clear-current-badge
cdx-care --json diagnose blank-page --out-dir /tmp/cdx-care-blank-page-new
cdx-care --json raw sql --db codex-dev --query-file query.sql --readonly
```

Use `apply` only after quitting Codex. `doctor` and `prep` are safe while Codex
is open because they do not mutate Codex DBs. The canonical operator path is
now `prep → apply`: `prep` performs the pre-scan, writes a private plan under
`~/.codex/cdx-care/plans/`, summarizes actions/denials, and returns the exact
`apply_command` to run if approved. Default `doctor` output is summary-first;
use `cdx-care --json doctor --details --limit N` only when you need bounded row
lists or raw `lsof` handle rows. `plan --out` remains the low-level explicit
artifact form. `run --apply-approved` is only a no-interactive-review shortcut
for the already approved workstation policy; it still writes the generated
plan and runs the same apply gates.

`workstation` is conservative: it hides broken/non-navigable unread rows but
keeps valid automation review rows unread. If the desired user-visible outcome
is “clear the current automation badge”, use the explicit
`clear-current-badge` profile after reviewing that it will mark valid
`PENDING_REVIEW`/`ACCEPTED` run instances as read. `run --profile
clear-current-badge --apply-approved` is denied on purpose; generate the plan,
inspect it, then apply that exact plan.

All JSON success/error responses include at least `schema_version`, `tool`,
`version`, and `ok`. State-aware commands also include `support_root`, and
write commands include the generated/applied action lists plus the receipt path.

## Current v1 lanes

- Automations/badge: report unread run-instance count. The `workstation`
  profile preserves valid `PENDING_REVIEW`/`ACCEPTED` runs and marks read only
  broken/non-navigable rows. The explicit `clear-current-badge` profile also
  marks current valid, navigable review rows read.
- Memory: reset retryable terminal Stage 1 error jobs to claimable pending
  state and enqueue the native global consolidation job. Auth/401 failures are
  reported as credential/config blockers and are not blindly retried.
- `.DS_Store`: remove tracked Finder metadata from the memory git index and
  commit that exact untracking while keeping local ignored files.
- Sessions/history: rebuild `session_index.jsonl` after rollout-file alignment
  proof by merging distinct current thread titles from `state_5.sqlite` with
  latest valid legacy fallback names for threads that still have blank/default
  SQLite titles. Do not rewrite `history.jsonl`; it is reported as
  message-history drift only.
- Logs: compact `logs_2.sqlite` freelist pages with `VACUUM` when reclaimable
  space is above the threshold. The lane backs up DB/WAL/SHM first and records
  before/after page, count, and byte stats without log bodies.

## Development

```bash
uv run python -m compileall src tests
uv run python -m unittest discover -s tests
uv run ruff check
uv run basedpyright
```

Install/operator proof:

```bash
uv build
uv tool install --reinstall .
cd /tmp
command -v cdx-care
cdx-care --help
cdx-care --json doctor
```

Optional companion skill install for this Mac:

```bash
mkdir -p ~/.codex/skills/cdx-care
cp skills/cdx-care/SKILL.md ~/.codex/skills/cdx-care/SKILL.md
```

The CLI install does not auto-install the skill; the skill is included so future
agents have the same safe operating order when the operator chooses to install
it.

## License and public visibility

This repository is public for transparency and local operator reuse by its
owner. It is **not** an open-source grant. See `LICENSE`: all rights reserved
unless a later license says otherwise.

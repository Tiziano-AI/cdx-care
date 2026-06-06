# cdx-care

`cdx-care` is a local operator CLI for Codex app/CLI support-root state under
`~/.codex`. It is designed for repeatable, evidence-backed diagnosis and
guarded reconciliation of DB/state drift without editing vendor Codex source.

## Safety contract

- `doctor`, `plan`, `diagnose`, and `raw sql --readonly` are read-only for
  Codex DBs.
- `apply` and `run --apply-approved` deny DB writes when Codex has live handles
  open on the target DBs.
- Every DB write is planned first with exact keys, schema fingerprint, old-value
  preconditions, backup of DB/WAL/SHM, transaction, readback, and a receipt.
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
- Session/history reindex is diagnostic-only in v1.
- Log DB compaction is diagnostic-only in v1.

## Commands

```bash
cdx-care --json doctor
cdx-care --json plan --profile workstation --out /tmp/cdx-care-plan.json
cdx-care --json apply --plan /tmp/cdx-care-plan.json
cdx-care --json run --profile workstation --apply-approved
cdx-care --json diagnose blank-page --out-dir /tmp/cdx-care-blank-page-new
cdx-care --json raw sql --db codex-dev --query-file query.sql --readonly
```

Use `apply` only after quitting Codex. `doctor` is safe with Codex open. The
canonical write path is `doctor → plan → review → apply`. Default `doctor`
output is summary-first; use `cdx-care --json doctor --details --limit N` when
you need bounded row lists or raw `lsof` handle rows. `run --apply-approved` is
only the no-interactive-review shortcut for the already approved workstation
policy; it still writes the generated plan and runs the same apply gates.

All JSON success/error responses include at least `schema_version`, `tool`,
`version`, and `ok`. State-aware commands also include `support_root`, and
write commands include the generated/applied action lists plus the receipt path.

## Current v1 lanes

- Automations/badge: report unread run-instance count, preserve valid
  `PENDING_REVIEW` runs, and mark read only broken/non-navigable rows by
  default.
- Memory: reset terminal Stage 1 error jobs to claimable pending state and
  enqueue the native global consolidation job.
- `.DS_Store`: remove tracked Finder metadata from the memory git index while
  keeping local ignored files.
- Sessions/history: compare indexes and report drift only.
- Logs: report integrity and freelist stats only.

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

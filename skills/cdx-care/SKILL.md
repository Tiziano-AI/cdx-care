---
name: cdx-care
description: "Operate the local cdx-care CLI for Codex support-root doctoring, read-only diagnosis, guarded plan/apply reconciliation, and read-only raw SQL. Use when inspecting or repairing local Codex DB/state coherence on this Mac; not for editing upstream Codex source or running ad hoc SQL writes."
---

# cdx-care

Use `cdx-care` when the task concerns local Codex app/CLI state under the
operator's Codex support root: automation badge/read-state, memory job retries,
memory git `.DS_Store` hygiene, session/history/index drift, logs DB health, or
blank automations page evidence.

Do not use it to edit vendor/upstream Codex source, clear valid review runs
with the default profile, rewrite `history.jsonl`, or run raw SQL writes.

## First command

Start from any repo with:

```bash
command -v cdx-care
cdx-care --help
cdx-care --json prep --profile workstation
```

`prep` is the normal operator pre-scan. It is read-only for Codex DBs, writes a
private plan under `~/.codex/cdx-care/plans/`, summarizes actions and denials,
and returns the exact `apply_command` to run after review. `doctor` is also
read-only and safe while Codex is open. Treat source files, installed CLI
proof, live app UI, DB receipts, and rendered app behavior as separate
evidence layers. Bare `doctor` is summary-first; use
`cdx-care --json doctor --details --limit N` only when you need bounded row
details or raw `lsof` handle rows.

## Safe operating order

1. Run the pre-scan:

   ```bash
   cdx-care --json prep --profile workstation
   ```

2. Review `action_summary`, `denial_summary`, `plan_path`, and
   `operator_message`. Default policy is
   `hide broken only`: valid `PENDING_REVIEW`/`ACCEPTED` automation runs remain
   unread.
3. Quit Codex before any apply.
4. Apply only the exact generated plan by copying the returned
   `apply_command`, for example:

   ```bash
   cdx-care --json apply --plan /Users/tiziano/.codex/cdx-care/plans/<run_id>.json
   ```

   `plan --out` remains available for custom artifact paths, but future agents
   should not require operators to compose shell/Python review pipelines.

   `run --apply-approved` remains available only when the already approved
   workstation policy is acceptable without
   another interactive review, run it in one shot after Codex is closed:

   ```bash
   cdx-care --json run --profile workstation --apply-approved
   ```

   This is a no-interactive-review shortcut for the already approved workstation
   policy. It still writes the generated plan, denies on live DB handles, backs
   up DBs, revalidates plan targets, applies with old-value checks, and writes a
   receipt. If `lsof` is unavailable or ambiguous, writes deny until handle
   proof works again.

   If the operator explicitly wants the current automation badge cleared, use
   the review-first opt-in profile instead. It marks valid, navigable unread
   `PENDING_REVIEW`/`ACCEPTED` run instances read, not only broken rows:

   ```bash
   cdx-care --json prep --profile clear-current-badge
   cdx-care --json apply --plan /Users/tiziano/.codex/cdx-care/plans/<run_id>.json
   ```

   Do not use `run --profile clear-current-badge --apply-approved`; the CLI
   intentionally denies that shortcut so the valid badge rows are reviewed first.

5. Reopen Codex and rerun `cdx-care --json doctor` plus the user-visible app
   check. A DB receipt is not rendered UI proof.

## Diagnostics and raw reads

Blank automation page evidence pack:

```bash
cdx-care --json diagnose blank-page --out-dir /tmp/cdx-care-blank-page-new
```

The `--out-dir` path must not already exist; `cdx-care` creates the directory
privately and refuses to chmod or reuse an existing directory.

Read-only raw SQL:

```bash
cdx-care --json raw sql --db codex-dev --query-file query.sql --readonly
```

Raw SQL accepts one read-only statement only. It denies DML/DDL, `ATTACH`,
write pragmas, and multi-statement input. Raw SQL returns selected raw DB
values, so do not query private body/title/error columns unless the user
explicitly wants local raw evidence.

## Authorization boundaries

`apply` and `run --apply-approved` must deny DB writes while Codex has handles
open. They back up DB/WAL/SHM, verify schema and row preconditions, apply in
SQLite transactions, revalidate editable plan fields against the closed policy,
write receipts, and refuse reused run IDs or existing output files. Memory
auth/401 errors are blockers, not retry candidates. Do not bypass these gates
with manual SQL unless the user explicitly asks for a one-off outside the
product lane.

The workstation plan may also rebuild `session_index.jsonl` and compact
`logs_2.sqlite` when the closed policy proves those actions are currently safe.
The session lane writes only the legacy thread-name index after rollout-file
alignment proof: it replaces distinct `state_5.sqlite` titles and preserves
latest valid legacy fallback names for threads whose SQLite title is still
blank/default. `history.jsonl` is message history and remains diagnostic-only.

Managed `~/.codex/cdx-care/**` artifact directories for generated run plans,
backups, and receipts are forced private (`0700`), and generated files are
created `0600`. `diagnose --out-dir` is different: it must be a new
user-supplied directory and existing directories are refused unchanged.

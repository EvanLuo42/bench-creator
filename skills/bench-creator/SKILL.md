---
name: bench-creator
description: Maintain a private global AI-work benchmark across repositories by checkpointing code before AI mutation, harvesting reusable difficulties afterward, validating privacy and capture policy, and running candidates in restored evaluator-isolated workspaces. Use before the first file mutation in a substantive repository task when an initialized global Bench enables auto or suggest capture; use after surprising failures, hidden constraints, fragile integrations, or specialized verification; and use whenever the user asks to initialize, register, capture, curate, validate, export, promote, or run Bench cases. Never use a solved live workspace as candidate input.
---

# Capture Bench Cases

Maintain one global `ai-work-bench/v1` hub. Treat repositories as sources, not Bench containers.

## Resolve the command and hub

Resolve the absolute directory containing this `SKILL.md`; never assume the current working directory is the Skill directory.

- Windows: run `powershell -NoProfile -ExecutionPolicy Bypass -File <skill-root>/scripts/benchctl.ps1 ...`
- POSIX: run `sh <skill-root>/scripts/benchctl.sh ...`

Use `<benchctl>` below as shorthand for that absolute launcher command. Set `AI_WORK_BENCH_PYTHON` only when an explicit Python interpreter override is needed.

Resolve the Bench from `AI_WORK_BENCH_HOME`, then `$CODEX_HOME/benches/daily-work`, then `~/.codex/benches/daily-work`. Do not create a per-repository `.bench`. If `bench.json` is missing, initialize only when the user explicitly requests setup:

`<benchctl> init --bench <global-path> --name "Daily Work Bench" --mode suggest --workspace-mode snapshot`

## Before repository work

Ask the CLI for the policy decision before the first edit or generated artifact:

`<benchctl> policy decide --bench <global-path> --phase before`

- `skip`: continue without Bench work.
- `checkpoint`: read [references/checkpointing.md](references/checkpointing.md), then run `checkpoint start` with the returned trigger.
- For a user-requested capture, add `--explicit` to the decision and use trigger `explicit`.

Keep the returned checkpoint and task IDs. If checkpointing fails, continue the parent task without capture and never reconstruct input from the solved workspace unless a verified historical parent exists.

## After repository work

Collect only signals that actually occurred, then ask for the final decision:

`<benchctl> policy decide --bench <global-path> --phase after --signal <signal> ...`

- `discard`: run `checkpoint discard`.
- `suggest`: ask for confirmation; capture with `--confirmed` only after approval.
- `capture`: read [references/case-authoring.md](references/case-authoring.md), create the case, and attach the checkpoint.

The CLI enforces minimum signals, suggestion confirmation, task-level case limits, privacy, and ready-case requirements. Use `--force-policy` only for an explicit user override; it never bypasses privacy validation.

## Author and curate

Keep solutions, final diffs, root causes, and evaluator commands out of `task`. Put reusable lessons in `difficulty` and deterministic checks in `evaluation`. Declare runtime/setup requirements in `task.environment`. Prefer hidden evaluator files for engineering cases.

Read only the reference needed for the operation:

- [references/case-authoring.md](references/case-authoring.md): case fields, signals, dedupe, fixtures, promotion.
- [references/runner.md](references/runner.md): environment contract, candidate protocol, checks, reports.
- [references/privacy-export.md](references/privacy-export.md): private storage, scanning, catalogs, approved runnable exports.
- [references/bench-format.md](references/bench-format.md): complete format when changing schemas or implementing an adapter.

Common curation commands are `project register`, `project list`, `list`, `validate`, `schema sync`, `promote`, `export`, and `run`. Prefer `run --candidate-json '["executable","arg"]'`; retain `--candidate` only for trusted shell commands.

## Finish the parent task

State Bench activity in at most one short sentence with project ID, case ID, and whether it was added, revised, drafted, discarded, or blocked. Do not claim a runnable case when the input state, environment, privacy review, or oracle is unreliable.

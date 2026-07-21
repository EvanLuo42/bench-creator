# AI Work Bench v1

## Contents

- [Global hub](#global-hub)
- [Project registry](#project-registry)
- [Before-task checkpoints](#before-task-checkpoints)
- [Manifest](#manifest)
- [Case](#case)
- [Workspace modes](#workspace-modes)
- [Hidden engineering evaluation](#hidden-engineering-evaluation)
- [Candidate protocol](#candidate-protocol)
- [Checks and scoring](#checks-and-scoring)
- [Privacy and portability](#privacy-and-portability)

`ai-work-bench/v1` is one global benchmark assembled from AI-assisted work across many repositories. Its metadata is versionable; private source objects remain local unless an explicit runnable export is approved. JSON is used so the hub works without optional YAML dependencies.

## Global hub

```text
<global-bench>/
  bench.json
  projects/<project-id>.json
  cases/<project-id>/<case-id>.json
  objects/sha256/<content-sha256>.tar.gz
  fixtures/<case-id>/...
  oracles/<project-id>/<case-id>/...
  checkpoints/<checkpoint-id>.json
  local/projects.json
  local/transactions/<transaction-id>.json
  schema/bench-manifest.schema.json
  schema/bench-case.schema.json
  reports/...
```

The default location is `AI_WORK_BENCH_HOME`, then `$CODEX_HOME/benches/daily-work`, then `~/.codex/benches/daily-work`. Repositories do not contain separate `.bench` directories.

`objects/sha256` is a content-addressed store, so identical pre-task workspaces are saved once even when several projects or cases reference them. Private initialization ignores `objects`, `oracles`, `fixtures`, `reports`, `checkpoints`, and `local` in the generated `.gitignore`. This is a safe default, not an access-control boundary.

## Project registry

`projects/<project-id>.json` contains shareable metadata only:

```json
{
  "schema_version": "ai-work-bench/project-v1",
  "id": "billing-api-a91d42c0",
  "display_name": "billing-api",
  "identity_kind": "git-remote",
  "identity_sha256": "<sha256>",
  "created_at": "2026-07-21T00:00:00Z",
  "updated_at": "2026-07-21T00:00:00Z"
}
```

Remote URLs and absolute paths are hashed, not stored in shareable project metadata. The private `local/projects.json` maps project IDs to current local repository paths. Snapshot cases do not need this mapping to run; `repo-ref` cases do.

## Before-task checkpoints

An engineering case must preserve the state before the AI starts modifying files:

```text
before-task workspace  -> candidate-visible input
candidate changes      -> measured diff
after-task oracle      -> hidden evaluator only
```

Create the checkpoint before the first mutation. Never capture an already-fixed workspace as `task.workspace`. If no reliable pre-task state exists, keep the case as a draft or reconstruct it from a verified historical parent.

The solved working tree is never used as the candidate directory. Every engineering run creates a temporary directory and restores either a snapshot or a Git revision into it.

## Manifest

```json
{
  "$schema": "./schema/bench-manifest.schema.json",
  "schema_version": "ai-work-bench/v1",
  "id": "daily-work",
  "name": "Daily Work Bench",
  "description": "Reusable hard cases captured across repositories.",
  "created_at": "2026-07-21T00:00:00Z",
  "updated_at": "2026-07-21T00:00:00Z",
  "capture_policy": {
    "mode": "auto",
    "minimum_signals": 2,
    "max_cases_per_task": 1,
    "require_redaction": true,
    "workspace_mode": "snapshot",
    "max_snapshot_mb": 100
  },
  "runner": {
    "protocol": "ai-work-bench/candidate-v1",
    "timeout_seconds": 120
  },
  "privacy": {
    "visibility": "private",
    "allow_source_export": false
  }
}
```

`capture_policy.mode` is `auto`, `suggest`, or `off`; new hubs default to `suggest`. Before work, both `auto` and `suggest` preserve a checkpoint, while `off` skips automatic capture. After work, `auto` captures qualifying cases, `suggest` requires confirmation, and `off` discards unless the user explicitly requested capture. The CLI enforces minimum signals and the task-level case limit using checkpoint trigger and task IDs.

`workspace_mode` is `snapshot`, `repo-ref`, or `off`. Snapshot is the safe general default because it preserves uncommitted pre-task work. Global JSON writes are serialized by an advisory lock; related Case, Checkpoint, and Manifest updates use a recoverable write-ahead transaction.

## Case

Each case has a global ID and a project namespace:

```json
{
  "$schema": "../../schema/bench-case.schema.json",
  "schema_version": "ai-work-bench/case-v1",
  "id": "fix-idempotency-race-73e2c168",
  "revision": 1,
  "status": "ready",
  "title": "Prevent duplicate writes under concurrent retries",
  "created_at": "2026-07-21T00:00:00Z",
  "updated_at": "2026-07-21T00:00:00Z",
  "tags": ["concurrency", "integration", "engineering"],
  "project": {
    "id": "billing-api-a91d42c0",
    "name": "billing-api"
  },
  "source": {
    "kind": "ai-work-session",
    "dedupe_key": "payments/idempotency/concurrent-retry",
    "privacy": "redacted",
    "summary": "A unit-only fix still duplicated writes during concurrent retries.",
    "capture_task_id": "task-7abca103c9b14a949ae7ecce3180d2ee",
    "capture_trigger": "auto"
  },
  "task": {
    "kind": "repo",
    "prompt": "Fix duplicate writes under concurrent retries without changing the public API.",
    "context": {},
    "constraints": ["Preserve the existing transaction boundary."],
    "fixtures": [],
    "environment": {
      "platforms": ["linux", "darwin"],
      "runtime": {"python": ">=3.11"},
      "network": "off",
      "variables": {},
      "services": ["postgres"],
      "preflight": [
        {"id": "python", "command": ["python", "--version"]}
      ],
      "setup": [
        {"id": "install", "command": ["python", "-m", "pip", "install", "-r", "requirements.txt"]}
      ]
    },
    "workspace": {
      "mode": "snapshot",
      "archive": "objects/sha256/<content-sha256>.tar.gz",
      "sha256": "<archive-sha256>",
      "content_sha256": "<content-sha256>",
      "root": "project",
      "file_count": 83,
      "bytes": 214003,
      "capture_phase": "before-task",
      "base_commit": "0123456789abcdef0123456789abcdef01234567",
      "working_tree_dirty": true
    }
  },
  "difficulty": {
    "signals": ["non-obvious-root-cause", "verification-gap"],
    "summary": "The bug only reproduced across a real transaction boundary.",
    "root_cause": "The idempotency check and write were not atomic.",
    "failed_approaches": ["Adding an in-process mutex"],
    "key_insight": "Evaluate the invariant at the storage boundary."
  },
  "evaluation": {
    "pass_threshold": 1.0,
    "fixtures": [
      {
        "source": "oracles/billing-api-a91d42c0/fix-idempotency-race-73e2c168/hidden_test.py",
        "target": "tests/.bench_hidden/test_retry_race.py",
        "sha256": "<sha256>"
      }
    ],
    "checks": [
      {
        "id": "regression-test",
        "type": "command",
        "command": ["pytest", "-q", "tests/.bench_hidden/test_retry_race.py"],
        "expected_exit_code": 0,
        "timeout_seconds": 120,
        "weight": 3
      },
      {
        "id": "scoped-diff",
        "type": "changed_paths",
        "allow": ["src/**", "tests/**"],
        "require": ["src/**"],
        "weight": 1
      }
    ]
  }
}
```

The runner sends `task` and project display metadata to the candidate. It withholds `difficulty`, `source`, `evaluation`, hidden fixtures, reports, and live repository paths.

## Workspace modes

### Snapshot

Use snapshot when the pre-task tree is dirty, contains untracked inputs, must be portable, or must remain runnable after the local repository moves.

The snapshot contains current file contents at checkpoint time, not just `base_commit`. Modified tracked files and non-ignored untracked files are included. Git metadata, `.bench`, dependencies, caches, build outputs, secrets, symbolic links, and configured `.benchignore` matches are excluded or rejected. Symbolic links are refused instead of being silently converted to regular files.

### Repo-ref

Use repo-ref only when the pre-task Git tree is clean. It records `base_commit` and a content fingerprint. At run time, the runner invokes `git archive <base_commit>` from the registered repository and restores it into a temporary directory. It never executes the candidate in the current checkout.

If pre-task changes are uncommitted, repo-ref is invalid; use snapshot. If the local repository or Git object is unavailable, the case cannot run until the project is remapped or converted to snapshot.

## Hidden engineering evaluation

After the candidate exits, the runner computes changes and copies the candidate result into a fresh evaluator workspace. `evaluation.fixtures` are injected only into that copy, and injection refuses to overwrite a candidate-created path. Candidate background descendants therefore do not receive the hidden files in their original workspace. This is evaluator separation, not an adversarial operating-system sandbox.

The runner calculates `changed_paths` before injecting evaluator files or running evaluator commands. Build output and standard dependency/cache directories are ignored by this diff calculation.

## Candidate protocol

The runner starts the trusted candidate command in the restored temporary workspace and writes one JSON request to standard input:

```json
{
  "protocol": "ai-work-bench/candidate-v1",
  "case_id": "example-id",
  "project": {"id": "project-id", "name": "project-name"},
  "bench_root": "../.bench-input",
  "workspace_root": ".",
  "task": {
    "kind": "repo",
    "prompt": "...",
    "context": {},
    "constraints": [],
    "fixtures": [],
    "environment": {"platforms": ["any"], "preflight": [], "setup": []},
    "workspace": {"mode": "snapshot"}
  }
}
```

Preferred result:

```json
{
  "protocol": "ai-work-bench/result-v1",
  "text": "Human-readable answer",
  "data": {},
  "artifacts": [{"path": "relative/output.txt"}],
  "metadata": {"model": "candidate identity"}
}
```

Plain stdout is treated as `text`. A JSON result with the wrong protocol or a non-zero candidate exit fails the case. Prefer a JSON argument array for the candidate command; shell strings remain a trusted backward-compatibility path. Temporary-directory isolation protects the live checkout from accidental mutation, not the host from hostile commands.

The runner validates the declared platform, then executes `preflight` and `setup` argument arrays before candidate change tracking. Failures in materialization, environment preparation, or evaluator preparation produce an `infrastructure_error` result and do not abort the remaining selected Cases.

## Checks and scoring

The automated score is passed weight divided by total automated weight. The case passes when the score meets `pass_threshold` and the candidate process exits successfully.

- `text_contains`: require a case-sensitive substring.
- `text_not_contains`: reject a substring.
- `text_regex`: match a Python regular expression.
- `json_pointer_equals`: compare a value in result `data`.
- `artifact_exists`: require a workspace-relative artifact.
- `artifact_sha256`: require an artifact digest.
- `command`: run a hidden argument-array command after evaluator fixture injection.
- `changed_paths`: enforce allowed and required file globs against candidate changes.
- `manual`: request human review and produce `needs_review`.

Exit code 0 means all selected cases passed, 1 means at least one failed or had an infrastructure error, and 2 means no failure occurred but human review remains.

## Privacy and portability

- Treat the global hub as private by default. `source.privacy` reviews Case metadata; it does not sanitize a source archive.
- Keep local mappings and transaction journals only under ignored `local/`.
- Never snapshot an already-solved tree as candidate input.
- Use `.benchignore` for generated or oversized inputs. Do not use it to conceal a required sensitive input.
- Refuse high-confidence secrets, private keys, credential files, symbolic links, and size-limit violations. Automated scanning is intentionally incomplete.
- Use metadata-only catalog export for sharing. It omits workspaces, evaluator commands, root causes, and failed approaches and defaults to synthetic Cases.
- Runnable source export is disabled by default and requires a Manifest opt-in, an acknowledgement flag, snapshot portability, and synthetic or explicitly approved source.

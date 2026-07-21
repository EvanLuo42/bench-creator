# Case Authoring

Read this reference before writing, capturing, revising, or promoting a Case.

## Candidate-visible boundary

`task` is sent to the candidate. Put only the requested outcome, necessary context, constraints, public fixtures, workspace descriptor, and environment contract there.

Never place the solution, final diff, root cause, failed attempts, evaluator command, or hidden oracle in `task`.

## Difficulty and policy

Use concrete signals that occurred, such as:

- `failed-approach`
- `non-obvious-root-cause`
- `hidden-requirement`
- `environment-constraint`
- `fragile-integration`
- `verification-gap`
- `reusable-tradeoff`

Automatic and suggested ready Cases must meet `capture_policy.minimum_signals`. Suggested capture also requires user confirmation. The checkpoint task ID lets the CLI enforce `max_cases_per_task`.

Use `hidden-requirement` only when an unstated or initially undiscovered product/environment requirement changed the solution. A visible requirement checked by a hidden deterministic oracle is normally `verification-gap`, not `hidden-requirement`.

Use a stable, project-scoped `source.dedupe_key` based on the invariant or failure mode, not transient filenames or timestamps.

## Evaluation

Prefer deterministic checks. For engineering tasks, combine a hidden `command` regression test with `changed_paths` when scope matters.

Store hidden files under `oracles/<project-id>/<case-id>/` and declare their target and digest in `evaluation.fixtures`. The runner copies candidate output into a separate evaluator workspace before injecting these files.

Use `manual` only when no credible deterministic oracle exists. Keep the Case as `draft` when the before-state, environment, privacy, or oracle is unreliable.

## Capture lifecycle

Write a draft JSON outside the hub, then attach the active checkpoint:

```text
<benchctl> capture --bench <hub> --input <draft.json> --checkpoint <checkpoint-id>
```

Add `--promote` only when the Case is immediately reproducible. Add `--confirmed` for an approved suggestion. Capture is transactional: Case, Checkpoint, and Manifest updates recover together after interruption.

Validate before promotion and after schema updates:

```text
<benchctl> validate --bench <hub>
<benchctl> promote --bench <hub> --id <case-id>
```

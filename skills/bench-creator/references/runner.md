# Runner and Environment

Read this reference before authoring environment steps, implementing a candidate adapter, or running Cases.

## Environment contract

`task.environment` is candidate-visible and contains:

- `platforms`: `any`, `windows`, `linux`, or `darwin`.
- `runtime`: descriptive runtime constraints such as `{"python": ">=3.11"}`.
- `network`: `off`, `optional`, or `required`; this is declared metadata, not an OS firewall.
- `variables`: nonsecret environment variables injected into steps, candidate, and evaluator.
- `services`: required external services.
- `preflight`: argument-array checks that verify prerequisites.
- `setup`: argument-array commands that prepare workspace-local dependencies.

Preflight and setup run before candidate change tracking. Avoid global package installation and host mutation; use workspace-local caches or environments. A failed step becomes `infrastructure_error` and does not abort remaining Cases.

## Candidate protocol

The runner writes one `ai-work-bench/candidate-v1` JSON request to stdin. The preferred output is one `ai-work-bench/result-v1` JSON object. Plain stdout remains compatible as text, but a JSON result with the wrong protocol fails the process gate.

Prefer an argument array to avoid shell quoting:

```text
<benchctl> run --bench <hub> --candidate-json '["candidate","--flag"]'
```

`--candidate` executes a trusted shell string for backward compatibility. Temporary workspaces protect the live checkout, not the host operating system; candidates and evaluator commands remain trusted local programs.

## Evaluator isolation and reports

After the candidate exits, the runner computes candidate changes, copies the resulting workspace to a fresh evaluator directory, and injects hidden fixtures only there. This prevents ordinary background descendants from observing fixture paths in the candidate workspace, but it is not a hostile-code sandbox.

Every selected Case produces a report entry. Materialization, platform, setup, and evaluator preparation failures use `infrastructure_error`; they do not discard earlier results or stop later Cases. Report filenames include a UUID suffix to avoid concurrent overwrite.

Exit codes are `0` for all passed, `1` for failure or infrastructure error, and `2` when only manual review remains.

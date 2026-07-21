# Checkpointing

Read this reference before creating a repository checkpoint.

## Trigger and task identity

Run the policy decision first. Pass its trigger to checkpointing:

```text
<benchctl> checkpoint start --bench <hub> --source <repo> --trigger auto|suggest|explicit
```

The CLI returns collision-resistant checkpoint and task IDs. Reuse the task ID only for checkpoints that belong to the same parent task. Global writes are serialized with an advisory lock and JSON state changes use a recoverable write-ahead journal.

`auto` requires manifest mode `auto`. `suggest` is refused when capture is off. `explicit` records a user-requested operation. `--force-policy` is reserved for an explicit override.

## Workspace mode

- Use `snapshot` by default. It preserves modified tracked files and non-ignored untracked files.
- Use `repo-ref` only for a clean Git tree with a durable local repository mapping.
- Never capture an already-fixed tree as candidate input.

Snapshots exclude Git metadata, dependencies, caches, build output, ignored files, and `.benchignore` matches. They refuse credential-like files, detected secrets, oversized inputs, and symbolic links. Exclude a refused nonessential path with `.benchignore`; do not suppress a finding when the file is required to reproduce the task.

Snapshot source is private by default. A content-addressed object can be shared by checkpoints without duplicating bytes, but it is still source disclosure.

## Failure and cleanup

Checkpoint failure must not block the parent coding task. Continue without automatic capture, retain the reason, and do not fall back to the solved workspace.

If the task does not produce a useful Case, discard the active checkpoint:

```text
<benchctl> checkpoint discard --bench <hub> --id <checkpoint-id>
```

Discard removes an unreferenced snapshot object. Captured checkpoints remain immutable evidence for their Case.

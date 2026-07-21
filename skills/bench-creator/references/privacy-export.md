# Privacy and Export

Read this reference before sharing, exporting, or weakening source protections.

## Private hub

Initialization defaults to `privacy.visibility=private`, `privacy.allow_source_export=false`, and capture mode `suggest`. Snapshots, oracles, fixtures, reports, checkpoints, and local mappings are ignored by the generated Git configuration.

`source.privacy` describes reviewed Case metadata; it does not automatically sanitize a repository snapshot. Secret scanning is a refusal gate for high-confidence credentials, not proof that source code contains no personal, customer, or proprietary information.

Keep runnable private source and shareable metadata as separate products.

## Catalog export

The default export creates a non-runnable, metadata-only catalog. It omits workspaces, evaluator commands, root causes, and failed approaches, and anonymizes project and Case IDs. Only synthetic Cases are included unless `--include-redacted` is explicitly supplied after review.

```text
<benchctl> export --bench <hub> --output <new-directory>
```

The destination must not already exist.

## Runnable export

Runnable export copies source snapshots, public fixtures, and hidden oracles. It therefore requires all of the following:

1. `privacy.allow_source_export=true` in the Manifest.
2. `--include-workspaces`.
3. `--acknowledge-source-disclosure`.
4. Every selected repository Case uses `snapshot`, not `repo-ref`.
5. Every selected Case is `synthetic` or explicitly `approved`; `redacted` is insufficient for source export.

Treat the exported directory as sensitive even when every gate passes. The CLI cannot infer repository ownership or contractual sharing rights.

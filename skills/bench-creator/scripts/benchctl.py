#!/usr/bin/env python3
"""Dependency-free CLI for ai-work-bench/v1 suites."""

from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import datetime as dt
import fnmatch
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
import uuid


MANIFEST_VERSION = "ai-work-bench/v1"
CASE_VERSION = "ai-work-bench/case-v1"
CANDIDATE_PROTOCOL = "ai-work-bench/candidate-v1"
RESULT_PROTOCOL = "ai-work-bench/result-v1"
REPORT_VERSION = "ai-work-bench/report-v1"
CATALOG_VERSION = "ai-work-bench/catalog-v1"
TRANSACTION_VERSION = "ai-work-bench/transaction-v1"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
CHECK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
CHECK_TYPES = {
    "text_contains",
    "text_not_contains",
    "text_regex",
    "json_pointer_equals",
    "artifact_exists",
    "artifact_sha256",
    "command",
    "changed_paths",
    "manual",
}
SECRET_PATTERNS = {
    "openai-api-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "npm-token": re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    "pypi-token": re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "bearer-token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "credential-url": re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "windows-home-path": re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s\"']+"),
    "unix-home-path": re.compile(r"/(?:home|Users)/[^/\s\"']+"),
}
FILE_SECRET_PATTERNS = {
    name: pattern
    for name, pattern in SECRET_PATTERNS.items()
    if name not in {"windows-home-path", "unix-home-path"}
}
CASE_ONLY_SENSITIVE_PATTERNS = {
    "remote-url": re.compile(r"(?i)\bhttps?://[^\s\"']+"),
    "email-address": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
}
DEFAULT_EXCLUDED_PARTS = {
    ".git",
    ".bench",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    ".next",
    ".turbo",
    "coverage",
    ".gradle",
}
SENSITIVE_FILE_PATTERNS = (
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "credentials.yml",
    "credentials.yaml",
    "secrets.json",
    "secrets.yml",
    "secrets.yaml",
    ".npmrc",
    ".pypirc",
    "npmrc",
)


class BenchError(Exception):
    pass


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str, fallback: str = "bench") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = slug[:64].rstrip("-")
    if len(slug) < 3:
        slug = fallback
    return slug


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def hash_value(value: Any) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(temporary, path)


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BenchError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BenchError(f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc


@contextmanager
def file_lock(path: Path, timeout_seconds: int = 30):
    """Acquire one cross-platform advisory lock without external dependencies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    locked = False
    try:
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise BenchError(f"timed out waiting for Bench write lock: {path}")
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def recover_transactions(bench: Path) -> int:
    transactions = bench / "local" / "transactions"
    if not transactions.is_dir():
        return 0
    recovered = 0
    for journal_path in sorted(transactions.glob("*.json")):
        journal = load_json(journal_path)
        if not isinstance(journal, dict) or journal.get("schema_version") != TRANSACTION_VERSION:
            raise BenchError(f"invalid transaction journal: {journal_path}")
        writes = journal.get("writes")
        if not isinstance(writes, list):
            raise BenchError(f"invalid transaction writes: {journal_path}")
        for item in writes:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or "value" not in item:
                raise BenchError(f"invalid transaction entry: {journal_path}")
            atomic_json(safe_child(bench, item["path"]), item["value"])
        journal_path.unlink()
        recovered += 1
    return recovered


@contextmanager
def bench_write_lock(bench: Path, timeout_seconds: int = 30):
    with file_lock(bench / "local" / ".write.lock", timeout_seconds=timeout_seconds):
        recover_transactions(bench)
        yield


def transactional_json_updates(bench: Path, updates: list[tuple[Path, Any]]) -> None:
    """Commit related JSON files with an idempotent write-ahead journal."""
    if not updates:
        return
    writes = []
    for path, value in updates:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(bench.resolve()).as_posix()
        except ValueError as exc:
            raise BenchError(f"transaction target escapes Bench: {path}") from exc
        writes.append({"path": relative, "value": value})
    transaction_id = "tx-" + uuid.uuid4().hex
    journal_path = bench / "local" / "transactions" / f"{transaction_id}.json"
    atomic_json(
        journal_path,
        {
            "schema_version": TRANSACTION_VERSION,
            "id": transaction_id,
            "created_at": now_utc(),
            "writes": writes,
        },
    )
    for path, value in updates:
        atomic_json(path, value)
    journal_path.unlink()


def default_bench_path() -> Path:
    configured = os.environ.get("AI_WORK_BENCH_HOME")
    if configured:
        return Path(configured).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "benches" / "daily-work"
    return Path.home() / ".codex" / "benches" / "daily-work"


def resolve_bench(root_value: str | Path | None, require_manifest: bool = True) -> Path:
    bench = Path(root_value).expanduser().resolve() if root_value else default_bench_path().resolve()
    if require_manifest and not (bench / "bench.json").is_file():
        raise BenchError(f"bench manifest not found: {bench / 'bench.json'}")
    return bench


def safe_child(base: Path, relative: str) -> Path:
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise BenchError(f"path escapes allowed root: {relative}") from exc
    return candidate


def deep_merge(base: Any, update: Any) -> Any:
    if isinstance(base, dict) and isinstance(update, dict):
        merged = copy.deepcopy(base)
        for key, value in update.items():
            merged[key] = deep_merge(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    return copy.deepcopy(update)


def scan_sensitive(value: Any) -> list[str]:
    text = json.dumps(value, ensure_ascii=False)
    patterns = {**SECRET_PATTERNS, **CASE_ONLY_SENSITIVE_PATTERNS}
    return [name for name, pattern in patterns.items() if pattern.search(text)]


def extra_fields(value: dict[str, Any], allowed: set[str], prefix: str) -> list[str]:
    extras = sorted(set(value) - allowed)
    return [f"{prefix} contains unsupported field {field!r}" for field in extras]


def valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_privacy_policy(case: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    if not isinstance(case, dict) or not isinstance(manifest, dict):
        return []
    policy = manifest.get("capture_policy", {})
    privacy = case.get("source", {}).get("privacy")
    if policy.get("require_redaction") and privacy not in {"redacted", "synthetic"}:
        return ["capture_policy.require_redaction permits only redacted or synthetic cases"]
    return []


def validate_capture_policy(
    case: dict[str, Any],
    manifest: dict[str, Any],
    bench: Path,
    *,
    exclude_case_id: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(case, dict) or case.get("status") == "retired":
        return errors
    source = case.get("source", {})
    trigger = source.get("capture_trigger", "explicit")
    policy = manifest.get("capture_policy", {})
    if trigger in {"auto", "suggest"}:
        minimum = int(policy.get("minimum_signals", 1))
        signals = case.get("difficulty", {}).get("signals", [])
        if not isinstance(signals, list) or len(signals) < minimum:
            errors.append(f"capture policy requires at least {minimum} difficulty signal(s) for {trigger} capture")
    if trigger == "suggest" and source.get("user_confirmed") is not True:
        errors.append("suggest capture requires explicit user confirmation")
    task_id = source.get("capture_task_id")
    if isinstance(task_id, str) and trigger != "explicit":
        maximum = int(policy.get("max_cases_per_task", 1))
        count = 0
        for _, existing in iter_cases(bench):
            if existing.get("id") == exclude_case_id:
                continue
            if existing.get("source", {}).get("capture_task_id") == task_id:
                count += 1
        if count >= maximum:
            errors.append(f"capture policy allows at most {maximum} case(s) for task {task_id}")
    return errors


def validate_manifest(manifest: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    errors.extend(
        extra_fields(
            manifest,
            {
                "$schema",
                "schema_version",
                "id",
                "name",
                "description",
                "created_at",
                "updated_at",
                "capture_policy",
                "runner",
                "privacy",
            },
            "manifest",
        )
    )
    if manifest.get("schema_version") != MANIFEST_VERSION:
        errors.append(f"schema_version must be {MANIFEST_VERSION!r}")
    if not isinstance(manifest.get("id"), str) or not ID_RE.fullmatch(manifest.get("id", "")):
        errors.append("id must match ^[a-z0-9][a-z0-9-]{2,63}$")
    if not isinstance(manifest.get("name"), str) or not manifest.get("name", "").strip():
        errors.append("name must be a non-empty string")
    if not isinstance(manifest.get("description"), str):
        errors.append("description must be a string")
    for field in ("created_at", "updated_at"):
        if not valid_timestamp(manifest.get(field)):
            errors.append(f"{field} must be a timezone-aware ISO 8601 timestamp")
    policy = manifest.get("capture_policy")
    if not isinstance(policy, dict):
        errors.append("capture_policy must be an object")
    else:
        errors.extend(
            extra_fields(
                policy,
                {
                    "mode",
                    "minimum_signals",
                    "max_cases_per_task",
                    "require_redaction",
                    "workspace_mode",
                    "max_snapshot_mb",
                },
                "capture_policy",
            )
        )
        if policy.get("mode") not in {"auto", "suggest", "off"}:
            errors.append("capture_policy.mode must be auto, suggest, or off")
        if not isinstance(policy.get("minimum_signals"), int) or not 1 <= policy.get("minimum_signals", 0) <= 9:
            errors.append("capture_policy.minimum_signals must be an integer from 1 to 9")
        if not isinstance(policy.get("max_cases_per_task"), int) or not 1 <= policy.get("max_cases_per_task", 0) <= 10:
            errors.append("capture_policy.max_cases_per_task must be an integer from 1 to 10")
        if not isinstance(policy.get("require_redaction"), bool):
            errors.append("capture_policy.require_redaction must be boolean")
        if policy.get("workspace_mode", "off") not in {"off", "repo-ref", "snapshot"}:
            errors.append("capture_policy.workspace_mode must be off, repo-ref, or snapshot")
        max_snapshot_mb = policy.get("max_snapshot_mb", 100)
        if not isinstance(max_snapshot_mb, int) or not 1 <= max_snapshot_mb <= 10240:
            errors.append("capture_policy.max_snapshot_mb must be an integer from 1 to 10240")
    runner = manifest.get("runner")
    if not isinstance(runner, dict):
        errors.append("runner must be an object")
    else:
        errors.extend(extra_fields(runner, {"protocol", "timeout_seconds"}, "runner"))
        if runner.get("protocol") != CANDIDATE_PROTOCOL:
            errors.append(f"runner.protocol must be {CANDIDATE_PROTOCOL!r}")
        timeout = runner.get("timeout_seconds")
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            errors.append("runner.timeout_seconds must be an integer from 1 to 3600")
    privacy = manifest.get("privacy")
    if privacy is not None:
        if not isinstance(privacy, dict):
            errors.append("privacy must be an object")
        else:
            errors.extend(extra_fields(privacy, {"visibility", "allow_source_export"}, "privacy"))
            if privacy.get("visibility", "private") not in {"private", "team"}:
                errors.append("privacy.visibility must be private or team")
            if not isinstance(privacy.get("allow_source_export", False), bool):
                errors.append("privacy.allow_source_export must be boolean")
    return errors


def validate_check(check: Any, index: int) -> list[str]:
    prefix = f"evaluation.checks[{index}]"
    errors: list[str] = []
    if not isinstance(check, dict):
        return [f"{prefix} must be an object"]
    errors.extend(
        extra_fields(
            check,
            {
                "id",
                "type",
                "expected",
                "pattern",
                "pointer",
                "path",
                "rubric",
                "weight",
                "command",
                "expected_exit_code",
                "timeout_seconds",
                "allow",
                "require",
            },
            prefix,
        )
    )
    if not isinstance(check.get("id"), str) or not CHECK_ID_RE.fullmatch(check.get("id", "")):
        errors.append(f"{prefix}.id has an invalid format")
    check_type = check.get("type")
    if check_type not in CHECK_TYPES:
        errors.append(f"{prefix}.type is unsupported")
        return errors
    if check_type != "manual":
        weight = check.get("weight", 1)
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            errors.append(f"{prefix}.weight must be a positive number")
    if check_type in {"text_contains", "text_not_contains"} and not isinstance(check.get("expected"), str):
        errors.append(f"{prefix}.expected must be a string")
    if check_type == "text_regex":
        if not isinstance(check.get("pattern"), str):
            errors.append(f"{prefix}.pattern must be a string")
        else:
            try:
                re.compile(check["pattern"])
            except re.error as exc:
                errors.append(f"{prefix}.pattern is invalid: {exc}")
    if check_type == "json_pointer_equals":
        pointer = check.get("pointer")
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
            errors.append(f"{prefix}.pointer must be empty or start with '/'")
    if check_type in {"artifact_exists", "artifact_sha256"}:
        artifact_path = check.get("path")
        if (
            not isinstance(artifact_path, str)
            or not artifact_path
            or Path(artifact_path).is_absolute()
            or ".." in Path(artifact_path).parts
        ):
            errors.append(f"{prefix}.path must stay inside the materialized workspace")
    if check_type == "artifact_sha256" and (
        not isinstance(check.get("expected"), str) or not SHA256_RE.fullmatch(check.get("expected", ""))
    ):
        errors.append(f"{prefix}.expected must be a lowercase SHA-256 digest")
    if check_type == "command":
        command = check.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item for item in command):
            errors.append(f"{prefix}.command must be a non-empty array of strings")
        if not isinstance(check.get("expected_exit_code", 0), int):
            errors.append(f"{prefix}.expected_exit_code must be an integer")
        if "timeout_seconds" in check and (
            not isinstance(check.get("timeout_seconds"), int) or not 1 <= check.get("timeout_seconds", 0) <= 3600
        ):
            errors.append(f"{prefix}.timeout_seconds must be an integer from 1 to 3600")
    if check_type == "changed_paths":
        for field in ("allow", "require"):
            patterns = check.get(field, [])
            if not isinstance(patterns, list) or any(not isinstance(item, str) or not item for item in patterns):
                errors.append(f"{prefix}.{field} must be an array of non-empty glob strings")
    if check_type == "manual" and not isinstance(check.get("rubric"), str):
        errors.append(f"{prefix}.rubric must be a string")
    return errors


def validate_environment(environment: Any) -> list[str]:
    prefix = "task.environment"
    errors: list[str] = []
    if not isinstance(environment, dict):
        return [f"{prefix} must be an object"]
    errors.extend(
        extra_fields(
            environment,
            {"platforms", "runtime", "network", "variables", "services", "preflight", "setup"},
            prefix,
        )
    )
    platforms = environment.get("platforms", ["any"])
    if (
        not isinstance(platforms, list)
        or not platforms
        or any(item not in {"any", "windows", "linux", "darwin"} for item in platforms)
        or ("any" in platforms and len(platforms) > 1)
    ):
        errors.append(f"{prefix}.platforms must contain 'any' or one or more supported platforms")
    runtime = environment.get("runtime", {})
    if not isinstance(runtime, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, str)
        for key, value in runtime.items()
    ):
        errors.append(f"{prefix}.runtime must map non-empty names to string constraints")
    if environment.get("network", "optional") not in {"off", "optional", "required"}:
        errors.append(f"{prefix}.network must be off, optional, or required")
    variables = environment.get("variables", {})
    if not isinstance(variables, dict) or any(
        not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        or not isinstance(value, str)
        for key, value in variables.items()
    ):
        errors.append(f"{prefix}.variables must map environment variable names to strings")
    services = environment.get("services", [])
    if not isinstance(services, list) or any(not isinstance(item, str) or not item for item in services):
        errors.append(f"{prefix}.services must be an array of non-empty strings")
    ids: list[str] = []
    for phase in ("preflight", "setup"):
        steps = environment.get(phase, [])
        if not isinstance(steps, list):
            errors.append(f"{prefix}.{phase} must be an array")
            continue
        for index, step in enumerate(steps):
            step_prefix = f"{prefix}.{phase}[{index}]"
            if not isinstance(step, dict):
                errors.append(f"{step_prefix} must be an object")
                continue
            errors.extend(
                extra_fields(step, {"id", "command", "expected_exit_code", "timeout_seconds"}, step_prefix)
            )
            step_id = step.get("id")
            if not isinstance(step_id, str) or not CHECK_ID_RE.fullmatch(step_id):
                errors.append(f"{step_prefix}.id has an invalid format")
            else:
                ids.append(step_id)
            command = step.get("command")
            if not isinstance(command, list) or not command or any(
                not isinstance(item, str) or not item for item in command
            ):
                errors.append(f"{step_prefix}.command must be a non-empty array of strings")
            if not isinstance(step.get("expected_exit_code", 0), int):
                errors.append(f"{step_prefix}.expected_exit_code must be an integer")
            timeout = step.get("timeout_seconds", 300)
            if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
                errors.append(f"{step_prefix}.timeout_seconds must be an integer from 1 to 3600")
    if len(ids) != len(set(ids)):
        errors.append(f"{prefix} step IDs must be unique across preflight and setup")
    return errors


def validate_workspace(workspace: Any, bench: Path) -> list[str]:
    prefix = "task.workspace"
    errors: list[str] = []
    if not isinstance(workspace, dict):
        return [f"{prefix} must be an object"]
    mode = workspace.get("mode")
    if mode == "snapshot":
        errors.extend(
            extra_fields(
                workspace,
                {
                    "mode",
                    "archive",
                    "sha256",
                    "content_sha256",
                    "root",
                    "subdir",
                    "file_count",
                    "bytes",
                    "capture_phase",
                    "base_commit",
                    "working_tree_dirty",
                },
                prefix,
            )
        )
        archive = workspace.get("archive")
        if not isinstance(archive, str) or re.fullmatch(r"objects/sha256/[a-f0-9]{64}\.tar\.gz", archive) is None:
            errors.append(f"{prefix}.archive must be an objects/sha256/<content-sha256>.tar.gz path")
        else:
            try:
                archive_path = safe_child(bench, archive)
            except BenchError as exc:
                errors.append(f"{prefix}.archive: {exc}")
            else:
                digest = workspace.get("sha256")
                if not archive_path.is_file():
                    errors.append(f"{prefix}.archive does not exist: {archive}")
                elif isinstance(digest, str) and SHA256_RE.fullmatch(digest) and hash_file(archive_path) != digest:
                    errors.append(f"{prefix}.sha256 does not match the archive")
        for field in ("sha256", "content_sha256"):
            if not isinstance(workspace.get(field), str) or not SHA256_RE.fullmatch(workspace.get(field, "")):
                errors.append(f"{prefix}.{field} must be a lowercase SHA-256 digest")
        if (
            isinstance(archive, str)
            and isinstance(workspace.get("content_sha256"), str)
            and archive != f"objects/sha256/{workspace['content_sha256']}.tar.gz"
        ):
            errors.append(f"{prefix}.archive filename must equal content_sha256")
        if workspace.get("root") != "project":
            errors.append(f"{prefix}.root must be 'project'")
        if not isinstance(workspace.get("file_count"), int) or workspace.get("file_count", -1) < 0:
            errors.append(f"{prefix}.file_count must be an integer >= 0")
        if not isinstance(workspace.get("bytes"), int) or workspace.get("bytes", -1) < 0:
            errors.append(f"{prefix}.bytes must be an integer >= 0")
    elif mode == "repo-ref":
        errors.extend(
            extra_fields(
                workspace,
                {
                    "mode",
                    "subdir",
                    "content_sha256",
                    "file_count",
                    "bytes",
                    "capture_phase",
                    "base_commit",
                    "working_tree_dirty",
                },
                prefix,
            )
        )
        if not isinstance(workspace.get("content_sha256"), str) or not SHA256_RE.fullmatch(
            workspace.get("content_sha256", "")
        ):
            errors.append(f"{prefix}.content_sha256 must lock the referenced working tree")
        if not isinstance(workspace.get("file_count"), int) or workspace.get("file_count", -1) < 0:
            errors.append(f"{prefix}.file_count must be an integer >= 0")
        if not isinstance(workspace.get("bytes"), int) or workspace.get("bytes", -1) < 0:
            errors.append(f"{prefix}.bytes must be an integer >= 0")
    else:
        errors.append(f"{prefix}.mode must be snapshot or repo-ref")
    if workspace.get("capture_phase") != "before-task":
        errors.append(f"{prefix}.capture_phase must be 'before-task' to prevent solution leakage")
    if "base_commit" in workspace and (
        not isinstance(workspace.get("base_commit"), str)
        or re.fullmatch(r"[0-9a-fA-F]{7,64}", workspace.get("base_commit", "")) is None
    ):
        errors.append(f"{prefix}.base_commit must be a 7-64 character hexadecimal revision")
    if "working_tree_dirty" in workspace and not isinstance(workspace.get("working_tree_dirty"), bool):
        errors.append(f"{prefix}.working_tree_dirty must be boolean")
    if mode == "repo-ref":
        if "base_commit" not in workspace:
            errors.append(f"{prefix}.base_commit is required for repo-ref")
        if workspace.get("working_tree_dirty") is not False:
            errors.append(f"{prefix}.working_tree_dirty must be false for repo-ref; use snapshot for dirty input")
    subdir = workspace.get("subdir", ".")
    if not isinstance(subdir, str) or not subdir:
        errors.append(f"{prefix}.subdir must be a non-empty relative path")
    elif Path(subdir).is_absolute() or ".." in Path(subdir).parts:
        errors.append(f"{prefix}.subdir must stay inside the materialized project")
    return errors


def validate_case(case: Any, bench: Path, ready_strict: bool | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(case, dict):
        return ["case must be a JSON object"]
    errors.extend(
        extra_fields(
            case,
            {
                "$schema",
                "schema_version",
                "id",
                "revision",
                "status",
                "title",
                "created_at",
                "updated_at",
                "tags",
                "project",
                "source",
                "task",
                "difficulty",
                "evaluation",
            },
            "case",
        )
    )
    if case.get("schema_version") != CASE_VERSION:
        errors.append(f"schema_version must be {CASE_VERSION!r}")
    case_id = case.get("id")
    if not isinstance(case_id, str) or not ID_RE.fullmatch(case_id):
        errors.append("id must match ^[a-z0-9][a-z0-9-]{2,63}$")
        case_id = "invalid"
    if not isinstance(case.get("revision"), int) or case.get("revision", 0) < 1:
        errors.append("revision must be an integer >= 1")
    if case.get("status") not in {"draft", "ready", "retired"}:
        errors.append("status must be draft, ready, or retired")
    if not isinstance(case.get("title"), str) or not case.get("title", "").strip():
        errors.append("title must be a non-empty string")
    for field in ("created_at", "updated_at"):
        if not valid_timestamp(case.get(field)):
            errors.append(f"{field} must be a timezone-aware ISO 8601 timestamp")
    tags = case.get("tags")
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag for tag in tags):
        errors.append("tags must be an array of non-empty strings")
    elif len(tags) != len(set(tags)):
        errors.append("tags must not contain duplicates")
    project = case.get("project")
    if not isinstance(project, dict):
        errors.append("project must be an object")
        project_id = "global"
    else:
        errors.extend(extra_fields(project, {"id", "name"}, "project"))
        project_id = project.get("id")
        if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
            errors.append("project.id must match ^[a-z0-9][a-z0-9-]{2,63}$")
            project_id = "global"
        if not isinstance(project.get("name"), str) or not project.get("name", "").strip():
            errors.append("project.name must be a non-empty string")
    source = case.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        errors.extend(
            extra_fields(
                source,
                {
                    "kind",
                    "dedupe_key",
                    "privacy",
                    "summary",
                    "capture_task_id",
                    "capture_trigger",
                    "user_confirmed",
                },
                "source",
            )
        )
        if source.get("kind") not in {"ai-work-session", "manual", "imported"}:
            errors.append("source.kind must be ai-work-session, manual, or imported")
        if not isinstance(source.get("dedupe_key"), str) or len(source.get("dedupe_key", "")) < 3:
            errors.append("source.dedupe_key must be a string of at least 3 characters")
        if source.get("privacy") not in {"redacted", "synthetic", "approved"}:
            errors.append("source.privacy must be redacted, synthetic, or approved")
        if not isinstance(source.get("summary"), str):
            errors.append("source.summary must be a string")
        if "capture_task_id" in source and (
            not isinstance(source.get("capture_task_id"), str)
            or not ID_RE.fullmatch(source.get("capture_task_id", ""))
        ):
            errors.append("source.capture_task_id has an invalid format")
        if source.get("capture_trigger", "explicit") not in {"auto", "suggest", "explicit", "imported"}:
            errors.append("source.capture_trigger must be auto, suggest, explicit, or imported")
        if "user_confirmed" in source and not isinstance(source.get("user_confirmed"), bool):
            errors.append("source.user_confirmed must be boolean")
    task = case.get("task")
    if not isinstance(task, dict):
        errors.append("task must be an object")
    else:
        errors.extend(
            extra_fields(
                task,
                {"kind", "prompt", "context", "constraints", "fixtures", "workspace", "environment"},
                "task",
            )
        )
        kind = task.get("kind", "response")
        if kind not in {"response", "repo"}:
            errors.append("task.kind must be response or repo")
        workspace = task.get("workspace")
        if kind == "repo" and workspace is None:
            errors.append("repo tasks require task.workspace")
        if workspace is not None:
            if kind != "repo":
                errors.append("task.workspace is only valid when task.kind is repo")
            errors.extend(validate_workspace(workspace, bench))
            if not (bench / "projects" / f"{project_id}.json").is_file():
                errors.append(f"project {project_id!r} is not registered in this global bench")
        if not isinstance(task.get("prompt"), str) or not task.get("prompt", "").strip():
            errors.append("task.prompt must be a non-empty string")
        if "context" not in task:
            errors.append("task.context is required")
        if "environment" in task:
            errors.extend(validate_environment(task["environment"]))
        constraints = task.get("constraints")
        if not isinstance(constraints, list) or any(not isinstance(item, str) for item in constraints):
            errors.append("task.constraints must be an array of strings")
        fixtures = task.get("fixtures")
        if not isinstance(fixtures, list):
            errors.append("task.fixtures must be an array")
        else:
            for index, fixture in enumerate(fixtures):
                prefix = f"task.fixtures[{index}]"
                if not isinstance(fixture, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                errors.extend(extra_fields(fixture, {"path", "sha256"}, prefix))
                fixture_path = fixture.get("path")
                expected_prefix = f"fixtures/{case_id}/"
                if not isinstance(fixture_path, str) or not fixture_path.startswith(expected_prefix):
                    errors.append(f"{prefix}.path must start with {expected_prefix!r}")
                    continue
                try:
                    actual_path = safe_child(bench, fixture_path)
                except BenchError as exc:
                    errors.append(f"{prefix}.path: {exc}")
                    continue
                digest = fixture.get("sha256")
                if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                    errors.append(f"{prefix}.sha256 must be a lowercase SHA-256 digest")
                elif actual_path.is_file() and hash_file(actual_path) != digest:
                    errors.append(f"{prefix} hash does not match {fixture_path}")
                elif not actual_path.is_file():
                    errors.append(f"{prefix} file does not exist: {fixture_path}")
    difficulty = case.get("difficulty")
    if not isinstance(difficulty, dict):
        errors.append("difficulty must be an object")
    else:
        errors.extend(
            extra_fields(
                difficulty,
                {"signals", "summary", "root_cause", "failed_approaches", "key_insight"},
                "difficulty",
            )
        )
        signals = difficulty.get("signals")
        if not isinstance(signals, list) or any(not isinstance(item, str) or not item for item in signals):
            errors.append("difficulty.signals must be an array of non-empty strings")
        elif len(signals) != len(set(signals)):
            errors.append("difficulty.signals must not contain duplicates")
        for field in ("summary", "root_cause", "key_insight"):
            if not isinstance(difficulty.get(field), str):
                errors.append(f"difficulty.{field} must be a string")
        failed = difficulty.get("failed_approaches")
        if not isinstance(failed, list) or any(not isinstance(item, str) for item in failed):
            errors.append("difficulty.failed_approaches must be an array of strings")
    evaluation = case.get("evaluation")
    checks: list[Any] = []
    if not isinstance(evaluation, dict):
        errors.append("evaluation must be an object")
    else:
        errors.extend(extra_fields(evaluation, {"pass_threshold", "checks", "fixtures"}, "evaluation"))
        threshold = evaluation.get("pass_threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 <= threshold <= 1:
            errors.append("evaluation.pass_threshold must be a number from 0 to 1")
        checks_value = evaluation.get("checks")
        if not isinstance(checks_value, list):
            errors.append("evaluation.checks must be an array")
        else:
            checks = checks_value
            for index, check in enumerate(checks):
                errors.extend(validate_check(check, index))
            check_ids = [check.get("id") for check in checks if isinstance(check, dict)]
            if len(check_ids) != len(set(check_ids)):
                errors.append("evaluation check IDs must be unique")
            if isinstance(task, dict) and "workspace" not in task:
                engineering_checks = [
                    check.get("id")
                    for check in checks
                    if isinstance(check, dict) and check.get("type") in {"command", "changed_paths"}
                ]
                if engineering_checks:
                    errors.append("command and changed_paths checks require an isolated task.workspace")
        evaluator_fixtures = evaluation.get("fixtures", [])
        if not isinstance(evaluator_fixtures, list):
            errors.append("evaluation.fixtures must be an array")
        else:
            expected_prefix = f"oracles/{project_id}/{case_id}/"
            for index, fixture in enumerate(evaluator_fixtures):
                prefix = f"evaluation.fixtures[{index}]"
                if not isinstance(fixture, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                errors.extend(extra_fields(fixture, {"source", "target", "sha256"}, prefix))
                source_value = fixture.get("source")
                if not isinstance(source_value, str) or not source_value.startswith(expected_prefix):
                    errors.append(f"{prefix}.source must start with {expected_prefix!r}")
                else:
                    try:
                        source_path = safe_child(bench, source_value)
                    except BenchError as exc:
                        errors.append(f"{prefix}.source: {exc}")
                    else:
                        digest = fixture.get("sha256")
                        if not source_path.is_file():
                            errors.append(f"{prefix}.source does not exist")
                        elif isinstance(digest, str) and SHA256_RE.fullmatch(digest) and hash_file(source_path) != digest:
                            errors.append(f"{prefix}.sha256 does not match source")
                target_value = fixture.get("target")
                if (
                    not isinstance(target_value, str)
                    or not target_value
                    or Path(target_value).is_absolute()
                    or ".." in Path(target_value).parts
                ):
                    errors.append(f"{prefix}.target must stay inside the materialized workspace")
                if not isinstance(fixture.get("sha256"), str) or not SHA256_RE.fullmatch(fixture.get("sha256", "")):
                    errors.append(f"{prefix}.sha256 must be a lowercase SHA-256 digest")
    strict = case.get("status") == "ready" if ready_strict is None else ready_strict
    if strict:
        if not checks:
            errors.append("ready cases require at least one evaluation check")
        if isinstance(difficulty, dict) and not difficulty.get("signals"):
            errors.append("ready cases require at least one difficulty signal")
        if isinstance(difficulty, dict) and not difficulty.get("root_cause", "").strip():
            errors.append("ready cases require a documented root cause")
        if isinstance(difficulty, dict) and not difficulty.get("key_insight", "").strip():
            errors.append("ready cases require a key insight")
    sensitive = scan_sensitive(case)
    if sensitive:
        errors.append("sensitive content detected: " + ", ".join(sensitive))
    return errors


def normalize_case(raw: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    case = deep_merge(previous or {}, raw)
    timestamp = now_utc()
    case["$schema"] = "../../schema/bench-case.schema.json"
    case["schema_version"] = CASE_VERSION
    case.setdefault("title", "Untitled captured case")
    case.setdefault("tags", [])
    case.setdefault("project", {"id": "global", "name": "Global"})
    case.setdefault("source", {})
    case["source"].setdefault("kind", "ai-work-session")
    case["source"].setdefault("privacy", "redacted")
    case["source"].setdefault("summary", "")
    case["source"].setdefault("capture_trigger", "explicit")
    case.setdefault("task", {})
    case["task"].setdefault("prompt", "")
    case["task"].setdefault("kind", "repo" if "workspace" in case["task"] else "response")
    case["task"].setdefault("context", {})
    case["task"].setdefault("constraints", [])
    case["task"].setdefault("fixtures", [])
    case["task"].setdefault(
        "environment",
        {
            "platforms": ["any"],
            "runtime": {},
            "network": "optional",
            "variables": {},
            "services": [],
            "preflight": [],
            "setup": [],
        },
    )
    case["source"].setdefault("dedupe_key", "sha256:" + hash_value(case["task"]))
    if not case.get("id"):
        case_identity = f"{case['project']['id']}:{case['source']['dedupe_key']}"
        fallback = "case-" + hash_value(case_identity)[:10]
        prefix = slugify(case.get("title", ""), fallback=fallback)
        case["id"] = (prefix[:52].rstrip("-") + "-" + hash_value(case_identity)[:10])[:64]
    case.setdefault("difficulty", {})
    case["difficulty"].setdefault("signals", [])
    case["difficulty"].setdefault("summary", "")
    case["difficulty"].setdefault("root_cause", "")
    case["difficulty"].setdefault("failed_approaches", [])
    case["difficulty"].setdefault("key_insight", "")
    case.setdefault("evaluation", {})
    case["evaluation"].setdefault("pass_threshold", 1.0)
    case["evaluation"].setdefault("checks", [])
    case["evaluation"].setdefault("fixtures", [])
    case.setdefault("status", "draft")
    if previous:
        case["created_at"] = previous.get("created_at", timestamp)
        case["revision"] = int(previous.get("revision", 0)) + 1
    else:
        case.setdefault("created_at", timestamp)
        case["revision"] = 1
    case["updated_at"] = timestamp
    return case


def iter_cases(bench: Path) -> list[tuple[Path, dict[str, Any]]]:
    entries: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((bench / "cases").rglob("*.json")):
        value = load_json(path)
        if not isinstance(value, dict):
            raise BenchError(f"case file must contain an object: {path}")
        entries.append((path, value))
    return entries


def find_existing(bench: Path, preliminary: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    wanted_id = preliminary.get("id")
    wanted_key = preliminary.get("source", {}).get("dedupe_key")
    wanted_project = preliminary.get("project", {}).get("id")
    key_match: tuple[Path, dict[str, Any]] | None = None
    for path, case in iter_cases(bench):
        if case.get("id") == wanted_id:
            if case.get("project", {}).get("id") != wanted_project:
                raise BenchError(f"case id {wanted_id!r} is already used by another project")
            return path, case
        if (
            wanted_key
            and case.get("project", {}).get("id") == wanted_project
            and case.get("source", {}).get("dedupe_key") == wanted_key
        ):
            if key_match is not None:
                raise BenchError(f"multiple cases share dedupe_key {wanted_key!r}")
            key_match = (path, case)
    return key_match


def git_command(source: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes] | None:
    git = shutil.which("git")
    if not git:
        return None
    return subprocess.run(
        [git, "-C", str(source), *arguments],
        capture_output=True,
        check=False,
    )


def git_state(source: Path) -> tuple[str | None, bool | None]:
    revision_process = git_command(source, ["rev-parse", "HEAD"])
    if revision_process is None or revision_process.returncode != 0:
        return None, None
    revision = revision_process.stdout.decode("utf-8", errors="replace").strip()
    status_process = git_command(source, ["status", "--porcelain", "--untracked-files=normal"])
    dirty = None if status_process is None or status_process.returncode != 0 else bool(status_process.stdout.strip())
    return revision or None, dirty


def discover_project_root(source: Path) -> Path:
    source = source.resolve()
    process = git_command(source, ["rev-parse", "--show-toplevel"])
    if process is not None and process.returncode == 0:
        value = process.stdout.decode("utf-8", errors="replace").strip()
        if value:
            return Path(value).resolve()
    return source


def project_identity(source: Path) -> tuple[str, str]:
    remote_process = git_command(source, ["config", "--get", "remote.origin.url"])
    if remote_process is not None and remote_process.returncode == 0:
        remote = remote_process.stdout.decode("utf-8", errors="replace").strip()
        if remote:
            return "git-remote", hashlib.sha256(remote.encode("utf-8")).hexdigest()
    return "local-path", hashlib.sha256(str(source.resolve()).casefold().encode("utf-8")).hexdigest()


def load_local_registry(bench: Path) -> dict[str, Any]:
    path = bench / "local" / "projects.json"
    if not path.is_file():
        return {"schema_version": "ai-work-bench/local-projects-v1", "projects": {}}
    value = load_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("projects"), dict):
        raise BenchError(f"invalid local project registry: {path}")
    return value


def register_project(
    bench: Path, source: Path, requested_id: str | None = None, requested_name: str | None = None
) -> dict[str, Any]:
    source = discover_project_root(source)
    identity_kind, identity_sha256 = project_identity(source)
    name = requested_name or source.name or "Project"
    project_id = requested_id or (
        slugify(name, fallback="project")[:54].rstrip("-") + "-" + identity_sha256[:8]
    )
    if not ID_RE.fullmatch(project_id):
        raise BenchError("project id must match ^[a-z0-9][a-z0-9-]{2,63}$")
    path = bench / "projects" / f"{project_id}.json"
    timestamp = now_utc()
    if path.is_file():
        public = load_json(path)
        if public.get("identity_sha256") != identity_sha256:
            raise BenchError(f"project id {project_id!r} is already registered to another repository")
        public["display_name"] = name
        public["updated_at"] = timestamp
    else:
        public = {
            "schema_version": "ai-work-bench/project-v1",
            "id": project_id,
            "display_name": name,
            "identity_kind": identity_kind,
            "identity_sha256": identity_sha256,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    local_path = bench / "local" / "projects.json"
    local = load_local_registry(bench)
    local["projects"][project_id] = {"path": str(source), "updated_at": timestamp}
    transactional_json_updates(bench, [(path, public), (local_path, local)])
    return public


def resolve_registered_project(bench: Path, project_id: str) -> Path:
    local = load_local_registry(bench)
    entry = local.get("projects", {}).get(project_id)
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        raise BenchError(f"project {project_id!r} has no local path mapping in local/projects.json")
    path = Path(entry["path"]).expanduser().resolve()
    if not path.is_dir():
        raise BenchError(f"registered local project path does not exist for {project_id!r}")
    return path


def command_project_register(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise BenchError(f"project source is not a directory: {source}")
    with bench_write_lock(bench):
        project = register_project(bench, source, args.id, args.name)
        update_manifest_timestamp(bench)
    print(json.dumps(project, ensure_ascii=False))
    return 0


def command_project_list(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    local = load_local_registry(bench)
    rows = []
    for path in sorted((bench / "projects").glob("*.json")):
        project = load_json(path)
        mapped = project.get("id") in local.get("projects", {})
        rows.append((project.get("id", path.stem), project.get("display_name", ""), "yes" if mapped else "no"))
    if not rows:
        print("no projects")
        return 0
    print("ID\tNAME\tLOCAL")
    for row in rows:
        print("\t".join(row))
    return 0


def read_benchignore(source: Path) -> list[str]:
    path = source / ".benchignore"
    if not path.is_file():
        return []
    patterns = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        value = raw.strip().replace("\\", "/")
        if value and not value.startswith("#"):
            if value.startswith("!"):
                raise BenchError(".benchignore negation patterns are not supported")
            patterns.append(value.lstrip("/"))
    return patterns


def ignored_workspace_path(relative: str, patterns: list[str]) -> bool:
    pure = Path(relative)
    if any(part in DEFAULT_EXCLUDED_PARTS for part in pure.parts):
        return True
    return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(pure.name, pattern) for pattern in patterns)


def candidate_workspace_paths(source: Path) -> list[str]:
    process = git_command(source, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if process is not None and process.returncode == 0:
        paths = [item.decode("utf-8", errors="surrogateescape") for item in process.stdout.split(b"\0") if item]
        return sorted(set(path.replace("\\", "/") for path in paths))
    paths: list[str] = []
    for current, directories, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if name not in DEFAULT_EXCLUDED_PARTS and not (current_path / name).is_symlink()
        ]
        for filename in filenames:
            paths.append((current_path / filename).relative_to(source).as_posix())
    return sorted(set(paths))


def select_workspace_files(source: Path, max_total_mb: int, max_file_mb: int) -> list[dict[str, Any]]:
    source = source.resolve()
    patterns = read_benchignore(source)
    max_total = max_total_mb * 1024 * 1024
    max_file = max_file_mb * 1024 * 1024
    selected: list[dict[str, Any]] = []
    sensitive: list[str] = []
    oversized: list[str] = []
    unsafe_links: list[str] = []
    total = 0
    for relative in candidate_workspace_paths(source):
        if ignored_workspace_path(relative, patterns):
            continue
        path = safe_child(source, relative)
        if path.is_symlink():
            unsafe_links.append(relative)
            continue
        if not path.is_file():
            continue
        lower_name = path.name.lower()
        if any(fnmatch.fnmatch(lower_name, pattern.lower()) for pattern in SENSITIVE_FILE_PATTERNS):
            sensitive.append(relative)
            continue
        size = path.stat().st_size
        if size > max_file:
            oversized.append(f"{relative} ({size} bytes)")
            continue
        total += size
        if total > max_total:
            raise BenchError(
                f"workspace exceeds {max_total_mb} MiB; add exclusions to .benchignore or raise --max-mb explicitly"
            )
        data = path.read_bytes()
        if b"\0" not in data[:8192]:
            text = data.decode("utf-8", errors="ignore")
            matches = [name for name, pattern in FILE_SECRET_PATTERNS.items() if pattern.search(text)]
            if matches:
                sensitive.append(f"{relative} ({', '.join(matches)})")
                continue
        file_mode = stat.S_IMODE(path.stat().st_mode)
        normalized_mode = 0o755 if file_mode & stat.S_IXUSR else 0o644
        selected.append(
            {
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": normalized_mode,
                "data": data,
            }
        )
    failures = []
    if sensitive:
        failures.append("sensitive files/content: " + ", ".join(sensitive[:20]))
    if oversized:
        failures.append("files exceed per-file limit: " + ", ".join(oversized[:20]))
    if unsafe_links:
        failures.append("symbolic links are not portable snapshot inputs: " + ", ".join(unsafe_links[:20]))
    if failures:
        raise BenchError("workspace snapshot refused; " + "; ".join(failures) + ". Exclude them with .benchignore.")
    if not selected:
        raise BenchError("workspace snapshot contains no files after exclusions")
    return selected


def tar_add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def workspace_content_manifest(files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "ai-work-bench/workspace-snapshot-v1",
        "root": "project",
        "files": [
            {"path": item["path"], "bytes": item["bytes"], "sha256": item["sha256"], "mode": item["mode"]}
            for item in files
        ],
    }


def create_workspace_archive(bench: Path, source: Path, max_total_mb: int, max_file_mb: int) -> dict[str, Any]:
    files = select_workspace_files(source, max_total_mb=max_total_mb, max_file_mb=max_file_mb)
    content_manifest = workspace_content_manifest(files)
    content_sha256 = hash_value(content_manifest)
    workspaces = bench / "objects" / "sha256"
    workspaces.mkdir(parents=True, exist_ok=True)
    archive_path = workspaces / f"{content_sha256}.tar.gz"
    if not archive_path.is_file():
        temporary = archive_path.with_name(archive_path.name + ".tmp")
        try:
            with temporary.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                    with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                        tar_add_bytes(archive, ".bench-snapshot.json", json_bytes(content_manifest))
                        for item in files:
                            tar_add_bytes(archive, f"project/{item['path']}", item["data"], item["mode"])
            os.replace(temporary, archive_path)
        finally:
            if temporary.exists():
                temporary.unlink()
    revision, dirty = git_state(source)
    descriptor: dict[str, Any] = {
        "mode": "snapshot",
        "archive": f"objects/sha256/{archive_path.name}",
        "sha256": hash_file(archive_path),
        "content_sha256": content_sha256,
        "root": "project",
        "file_count": len(files),
        "bytes": sum(item["bytes"] for item in files),
        "capture_phase": "before-task",
    }
    if revision:
        descriptor["base_commit"] = revision
    if dirty is not None:
        descriptor["working_tree_dirty"] = dirty
    return descriptor


def select_git_revision_files(
    source: Path, revision: str, max_total_mb: int, max_file_mb: int
) -> list[dict[str, Any]]:
    process = git_command(source, ["archive", "--format=tar", revision])
    if process is None or process.returncode != 0:
        message = "git archive failed"
        if process is not None:
            message = process.stderr.decode("utf-8", errors="replace").strip() or message
        raise BenchError(f"cannot materialize Git revision: {message}")
    with tempfile.TemporaryDirectory(prefix="awb-git-ref-") as temporary:
        extracted = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(process.stdout), mode="r:") as archive:
            extract_tar_safely(archive, extracted)
        return select_workspace_files(extracted, max_total_mb=max_total_mb, max_file_mb=max_file_mb)


def create_repo_ref_descriptor(source: Path, max_total_mb: int, max_file_mb: int) -> dict[str, Any]:
    source = source.resolve()
    revision, dirty = git_state(source)
    if not revision:
        raise BenchError("repo-ref requires a Git repository with a resolvable HEAD; use snapshot instead")
    if dirty:
        raise BenchError("repo-ref requires a clean before-task working tree; use snapshot to preserve uncommitted state")
    files = select_git_revision_files(
        source,
        revision,
        max_total_mb=max_total_mb,
        max_file_mb=max_file_mb,
    )
    content_manifest = workspace_content_manifest(files)
    descriptor: dict[str, Any] = {
        "mode": "repo-ref",
        "content_sha256": hash_value(content_manifest),
        "file_count": len(files),
        "bytes": sum(item["bytes"] for item in files),
        "capture_phase": "before-task",
    }
    descriptor["base_commit"] = revision
    descriptor["working_tree_dirty"] = False
    return descriptor


def load_checkpoint(bench: Path, checkpoint_id: str) -> tuple[Path, dict[str, Any]]:
    if not ID_RE.fullmatch(checkpoint_id):
        raise BenchError("checkpoint id has an invalid format")
    path = bench / "checkpoints" / f"{checkpoint_id}.json"
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != "ai-work-bench/checkpoint-v1":
        raise BenchError(f"invalid checkpoint: {path}")
    return path, value


def command_checkpoint_start(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    with bench_write_lock(bench):
        manifest = load_json(bench / "bench.json")
        manifest_errors = validate_manifest(manifest)
        if manifest_errors:
            raise BenchError("manifest validation failed: " + "; ".join(manifest_errors))
        trigger = getattr(args, "trigger", "explicit")
        force_policy = bool(getattr(args, "force_policy", False))
        policy_mode = manifest["capture_policy"].get("mode", "off")
        if not force_policy and trigger == "auto" and policy_mode != "auto":
            raise BenchError(f"automatic checkpoint refused because capture_policy.mode is {policy_mode!r}")
        if not force_policy and trigger == "suggest" and policy_mode == "off":
            raise BenchError("suggested checkpoint refused because capture_policy.mode is 'off'")
        mode = getattr(args, "mode", None) or manifest["capture_policy"].get("workspace_mode", "off")
        if mode == "off":
            raise BenchError("workspace checkpointing is off; set capture_policy.workspace_mode or pass --mode")
        source = discover_project_root(Path(args.source).expanduser().resolve())
        if not source.is_dir():
            raise BenchError(f"snapshot source is not a directory: {source}")
        project_record = register_project(bench, source, args.project_id, args.project_name)
        max_total_mb = args.max_mb or int(manifest["capture_policy"].get("max_snapshot_mb", 100))
        if mode == "snapshot":
            descriptor = create_workspace_archive(
                bench,
                source,
                max_total_mb=max_total_mb,
                max_file_mb=args.max_file_mb,
            )
        else:
            descriptor = create_repo_ref_descriptor(
                source,
                max_total_mb=max_total_mb,
                max_file_mb=args.max_file_mb,
            )
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ").lower()
        checkpoint_id = f"cp-{stamp}-{uuid.uuid4().hex[:12]}"
        task_id = getattr(args, "task_id", None) or f"task-{uuid.uuid4().hex}"
        if not ID_RE.fullmatch(task_id):
            raise BenchError("task id must match ^[a-z0-9][a-z0-9-]{2,63}$")
        checkpoint = {
            "schema_version": "ai-work-bench/checkpoint-v1",
            "id": checkpoint_id,
            "task_id": task_id,
            "trigger": trigger,
            "status": "active",
            "created_at": now_utc(),
            "updated_at": now_utc(),
            "project": {"id": project_record["id"], "name": project_record["display_name"]},
            "workspace": descriptor,
            "host_environment": {
                "platform": "windows" if sys.platform.startswith("win") else "darwin" if sys.platform == "darwin" else "linux",
                "python": platform.python_version(),
            },
        }
        checkpoint_path = bench / "checkpoints" / f"{checkpoint_id}.json"
        manifest["updated_at"] = now_utc()
        transactional_json_updates(
            bench,
            [(checkpoint_path, checkpoint), (bench / "bench.json", manifest)],
        )
    print(
        json.dumps(
            {
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "trigger": trigger,
                "project": checkpoint["project"],
                "workspace": descriptor,
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_checkpoint_list(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    rows = []
    for path in sorted((bench / "checkpoints").glob("*.json")):
        checkpoint = load_json(path)
        if args.status and checkpoint.get("status") != args.status:
            continue
        rows.append(
            (
                checkpoint.get("id", path.stem),
                checkpoint.get("project", {}).get("id", ""),
                checkpoint.get("status", ""),
                checkpoint.get("workspace", {}).get("mode", ""),
                checkpoint.get("created_at", ""),
            )
        )
    if not rows:
        print("no checkpoints")
        return 0
    print("ID\tPROJECT\tSTATUS\tMODE\tCREATED")
    for row in rows:
        print("\t".join(row))
    return 0


def command_checkpoint_discard(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    removed_object = False
    with bench_write_lock(bench):
        path, checkpoint = load_checkpoint(bench, args.id)
        if checkpoint.get("status") == "captured":
            raise BenchError("captured checkpoints cannot be discarded")
        checkpoint["status"] = "discarded"
        checkpoint["updated_at"] = now_utc()
        manifest = load_json(bench / "bench.json")
        manifest["updated_at"] = now_utc()
        transactional_json_updates(bench, [(path, checkpoint), (bench / "bench.json", manifest)])
        archive = checkpoint.get("workspace", {}).get("archive")
        if isinstance(archive, str):
            referenced = any(
                case.get("task", {}).get("workspace", {}).get("archive") == archive
                for _, case in iter_cases(bench)
            )
            if not referenced:
                for other_path in (bench / "checkpoints").glob("*.json"):
                    if other_path == path:
                        continue
                    other = load_json(other_path)
                    if (
                        other.get("status") in {"active", "captured"}
                        and other.get("workspace", {}).get("archive") == archive
                    ):
                        referenced = True
                        break
            if not referenced:
                archive_path = safe_child(bench, archive)
                if archive_path.is_file():
                    archive_path.unlink()
                    removed_object = True
    print(json.dumps({"checkpoint_id": args.id, "status": "discarded", "removed_object": removed_object}))
    return 0


def update_manifest_timestamp(bench: Path) -> None:
    path = bench / "bench.json"
    manifest = load_json(path)
    manifest["updated_at"] = now_utc()
    atomic_json(path, manifest)


def command_init(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench, require_manifest=False)
    lock_path = bench.parent / f".{bench.name}.init.lock"
    with file_lock(lock_path):
        return initialize_bench(args, bench)


def initialize_bench(args: argparse.Namespace, bench: Path) -> int:
    manifest_path = bench / "bench.json"
    if manifest_path.exists():
        raise BenchError(f"bench already exists: {manifest_path}")
    timestamp = now_utc()
    bench_id = slugify(args.id or args.name or bench.name, fallback="work-bench")
    if not ID_RE.fullmatch(bench_id):
        raise BenchError("bench id must match ^[a-z0-9][a-z0-9-]{2,63}$")
    manifest = {
        "$schema": "./schema/bench-manifest.schema.json",
        "schema_version": MANIFEST_VERSION,
        "id": bench_id,
        "name": args.name or "Daily Work Bench",
        "description": args.description or "Reusable hard cases captured from daily AI work.",
        "created_at": timestamp,
        "updated_at": timestamp,
        "capture_policy": {
            "mode": args.mode,
            "minimum_signals": args.minimum_signals,
            "max_cases_per_task": args.max_cases_per_task,
            "require_redaction": True,
            "workspace_mode": args.workspace_mode,
            "max_snapshot_mb": args.max_snapshot_mb,
        },
        "runner": {"protocol": CANDIDATE_PROTOCOL, "timeout_seconds": args.timeout},
        "privacy": {
            "visibility": getattr(args, "visibility", "private"),
            "allow_source_export": bool(getattr(args, "allow_source_export", False)),
        },
    }
    errors = validate_manifest(manifest)
    if errors:
        raise BenchError("invalid manifest: " + "; ".join(errors))
    for directory in (
        "cases",
        "fixtures",
        "oracles",
        "objects/sha256",
        "checkpoints",
        "projects",
        "local",
        "schema",
        "reports",
        "local/transactions",
    ):
        (bench / directory).mkdir(parents=True, exist_ok=True)
    schema_source = Path(__file__).resolve().parent.parent / "assets" / "schema"
    for name in ("bench-manifest.schema.json", "bench-case.schema.json"):
        source = schema_source / name
        if not source.is_file():
            raise BenchError(f"bundled schema missing: {source}")
        shutil.copyfile(source, bench / "schema" / name)
    atomic_json(manifest_path, manifest)
    (bench / ".gitignore").write_text(
        "reports/\nlocal/\ncheckpoints/\nobjects/\noracles/\nfixtures/\nexports/\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"initialized {MANIFEST_VERSION} at {bench}")
    return 0


def command_capture(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    with bench_write_lock(bench):
        result = capture_case_locked(args, bench)
    print(json.dumps(result))
    return 0


def capture_case_locked(args: argparse.Namespace, bench: Path) -> dict[str, Any]:
    manifest = load_json(bench / "bench.json")
    manifest_errors = validate_manifest(manifest)
    if manifest_errors:
        raise BenchError("manifest validation failed: " + "; ".join(manifest_errors))
    raw = load_json(Path(args.input).expanduser().resolve())
    if not isinstance(raw, dict):
        raise BenchError("capture input must be a JSON object")
    checkpoint_path: Path | None = None
    checkpoint: dict[str, Any] | None = None
    if args.checkpoint:
        checkpoint_path, checkpoint = load_checkpoint(bench, args.checkpoint)
        if checkpoint.get("status") != "active":
            raise BenchError(f"checkpoint is not active: {args.checkpoint}")
        raw = copy.deepcopy(raw)
        raw.setdefault("task", {})
        raw.setdefault("source", {})
        if not isinstance(raw["task"], dict) or not isinstance(raw["source"], dict):
            raise BenchError("capture input task and source must be objects")
        if "project" in raw and raw["project"] != checkpoint.get("project"):
            raise BenchError("capture input project does not match the checkpoint project")
        raw["project"] = checkpoint["project"]
        if "workspace" in raw["task"]:
            raise BenchError("capture input already defines task.workspace; omit it when using --checkpoint")
        raw["task"]["kind"] = "repo"
        raw["task"]["workspace"] = checkpoint["workspace"]
        raw["source"]["capture_task_id"] = checkpoint.get("task_id", f"task-{uuid.uuid4().hex}")
        raw["source"]["capture_trigger"] = checkpoint.get("trigger", "explicit")
        if bool(getattr(args, "confirmed", False)):
            raw["source"]["user_confirmed"] = True
    elif "project" not in raw and isinstance(raw.get("id"), str):
        try:
            _, existing_case = get_case_by_id(bench, raw["id"])
        except BenchError:
            pass
        else:
            raw = copy.deepcopy(raw)
            raw["project"] = existing_case["project"]
    preliminary = normalize_case(raw)
    match = find_existing(bench, preliminary)
    if match:
        existing_path, previous = match
        raw = copy.deepcopy(raw)
        raw["id"] = previous["id"]
        if "status" not in raw:
            raw["status"] = previous.get("status", "draft")
        case = normalize_case(raw, previous=previous)
        destination = existing_path
        action = "revised"
    else:
        case = preliminary
        destination = bench / "cases" / case["project"]["id"] / f"{case['id']}.json"
        action = "added"
    if args.promote:
        case["status"] = "ready"
    errors = validate_case(case, bench, ready_strict=case.get("status") == "ready")
    errors.extend(validate_privacy_policy(case, manifest))
    if not bool(getattr(args, "force_policy", False)):
        errors.extend(validate_capture_policy(case, manifest, bench, exclude_case_id=case.get("id")))
    if errors:
        raise BenchError("case validation failed:\n- " + "\n- ".join(errors))
    updates: list[tuple[Path, Any]] = [(destination, case)]
    if checkpoint_path is not None and checkpoint is not None:
        checkpoint["status"] = "captured"
        checkpoint["case_id"] = case["id"]
        checkpoint["updated_at"] = now_utc()
        updates.append((checkpoint_path, checkpoint))
    manifest["updated_at"] = now_utc()
    updates.append((bench / "bench.json", manifest))
    transactional_json_updates(bench, updates)
    return {"action": action, "id": case["id"], "revision": case["revision"], "status": case["status"]}


def get_case_by_id(bench: Path, case_id: str) -> tuple[Path, dict[str, Any]]:
    for path, case in iter_cases(bench):
        if case.get("id") == case_id:
            return path, case
    raise BenchError(f"case not found: {case_id}")


def command_promote(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    with bench_write_lock(bench):
        manifest = load_json(bench / "bench.json")
        manifest_errors = validate_manifest(manifest)
        if manifest_errors:
            raise BenchError("manifest validation failed: " + "; ".join(manifest_errors))
        path, case = get_case_by_id(bench, args.id)
        candidate = copy.deepcopy(case)
        candidate["status"] = "ready"
        candidate["revision"] = int(case.get("revision", 0)) + 1
        candidate["updated_at"] = now_utc()
        errors = validate_case(candidate, bench, ready_strict=True)
        errors.extend(validate_privacy_policy(candidate, manifest))
        if not bool(getattr(args, "force_policy", False)):
            errors.extend(validate_capture_policy(candidate, manifest, bench, exclude_case_id=candidate.get("id")))
        if errors:
            raise BenchError("cannot promote case:\n- " + "\n- ".join(errors))
        manifest["updated_at"] = now_utc()
        transactional_json_updates(bench, [(path, candidate), (bench / "bench.json", manifest)])
    print(json.dumps({"action": "promoted", "id": candidate["id"], "revision": candidate["revision"], "status": "ready"}))
    return 0


def command_validate(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    errors: list[str] = []
    manifest = load_json(bench / "bench.json")
    errors.extend(f"bench.json: {item}" for item in validate_manifest(manifest))
    project_ids: set[str] = set()
    for project_path in sorted((bench / "projects").glob("*.json")):
        project = load_json(project_path)
        if not isinstance(project, dict):
            errors.append(f"{project_path.name}: project record must be an object")
            continue
        project_id = project.get("id")
        if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
            errors.append(f"{project_path.name}: invalid project id")
            continue
        project_ids.add(project_id)
        if project_path.stem != project_id:
            errors.append(f"{project_path.name}: filename must equal project id + '.json'")
        if not isinstance(project.get("display_name"), str) or not project.get("display_name", "").strip():
            errors.append(f"{project_path.name}: display_name must be non-empty")
        if project.get("identity_kind") not in {"git-remote", "local-path"}:
            errors.append(f"{project_path.name}: invalid identity_kind")
        if not isinstance(project.get("identity_sha256"), str) or not SHA256_RE.fullmatch(
            project.get("identity_sha256", "")
        ):
            errors.append(f"{project_path.name}: invalid identity_sha256")
    local = load_local_registry(bench)
    for project_id in local.get("projects", {}):
        if project_id not in project_ids:
            errors.append(f"local/projects.json maps unregistered project {project_id!r}")
    seen_ids: dict[str, Path] = {}
    seen_keys: dict[tuple[str, str], Path] = {}
    count = 0
    for path, case in iter_cases(bench):
        count += 1
        if args.id and case.get("id") != args.id:
            continue
        if args.project and case.get("project", {}).get("id") != args.project:
            continue
        for item in validate_case(case, bench):
            errors.append(f"{path.name}: {item}")
        for item in validate_privacy_policy(case, manifest):
            errors.append(f"{path.name}: {item}")
        for item in validate_capture_policy(case, manifest, bench, exclude_case_id=case.get("id")):
            errors.append(f"{path.name}: {item}")
        if path.stem != case.get("id"):
            errors.append(f"{path.name}: filename must equal id + '.json'")
        if path.parent.name != case.get("project", {}).get("id"):
            errors.append(f"{path.name}: parent directory must equal project.id")
        case_id = case.get("id")
        if isinstance(case_id, str):
            if case_id in seen_ids:
                errors.append(f"{path.name}: duplicate id also used by {seen_ids[case_id].name}")
            seen_ids[case_id] = path
        key = case.get("source", {}).get("dedupe_key")
        if isinstance(key, str):
            scoped_key = (case.get("project", {}).get("id", "global"), key)
            if scoped_key in seen_keys:
                errors.append(f"{path.name}: duplicate project-scoped dedupe_key also used by {seen_keys[scoped_key].name}")
            seen_keys[scoped_key] = path
    if args.id and args.id not in seen_ids:
        errors.append(f"case not found: {args.id}")
    for schema_name in ("bench-manifest.schema.json", "bench-case.schema.json"):
        schema_path = bench / "schema" / schema_name
        bundled_path = Path(__file__).resolve().parent.parent / "assets" / "schema" / schema_name
        if not schema_path.is_file():
            errors.append(f"missing copied schema: schema/{schema_name}")
        else:
            try:
                load_json(schema_path)
            except BenchError as exc:
                errors.append(str(exc))
            if bundled_path.is_file() and hash_file(schema_path) != hash_file(bundled_path):
                errors.append(f"schema/{schema_name} differs from the bundled authoritative schema; reinitialize or sync it")
    if errors:
        print("validation failed", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"valid {MANIFEST_VERSION}: {count} case(s)")
    return 0


def command_list(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    rows = []
    for _, case in iter_cases(bench):
        if args.project and case.get("project", {}).get("id") != args.project:
            continue
        if args.status and case.get("status") != args.status:
            continue
        rows.append(
            (
                case.get("project", {}).get("id", ""),
                case.get("id", ""),
                case.get("status", ""),
                str(case.get("revision", "")),
                case.get("title", ""),
            )
        )
    if not rows:
        print("no cases")
        return 0
    print("PROJECT\tID\tSTATUS\tREV\tTITLE")
    for row in rows:
        print("\t".join(row))
    return 0


def command_policy_show(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    manifest = load_json(bench / "bench.json")
    errors = validate_manifest(manifest)
    if errors:
        raise BenchError("manifest validation failed: " + "; ".join(errors))
    print(json.dumps({"capture_policy": manifest["capture_policy"], "privacy": manifest.get("privacy", {})}))
    return 0


def command_policy_decide(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    manifest = load_json(bench / "bench.json")
    errors = validate_manifest(manifest)
    if errors:
        raise BenchError("manifest validation failed: " + "; ".join(errors))
    policy = manifest["capture_policy"]
    mode = policy["mode"]
    signals = sorted(set(args.signal or []))
    explicit = bool(args.explicit)
    if args.phase == "before":
        if explicit:
            action = "checkpoint"
            trigger = "explicit"
        elif mode == "off":
            action = "skip"
            trigger = "auto"
        else:
            action = "checkpoint"
            trigger = mode
        result = {"phase": "before", "action": action, "trigger": trigger, "mode": mode}
    else:
        minimum = int(policy["minimum_signals"])
        enough = explicit or len(signals) >= minimum
        if not enough or (mode == "off" and not explicit):
            action = "discard"
        elif explicit or mode == "auto":
            action = "capture"
        else:
            action = "suggest"
        result = {
            "phase": "after",
            "action": action,
            "mode": mode,
            "signals": signals,
            "minimum_signals": minimum,
            "max_cases_per_task": int(policy["max_cases_per_task"]),
        }
    print(json.dumps(result))
    return 0


def command_schema_sync(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    with bench_write_lock(bench):
        schema_source = Path(__file__).resolve().parent.parent / "assets" / "schema"
        copied = []
        for name in ("bench-manifest.schema.json", "bench-case.schema.json"):
            source = schema_source / name
            if not source.is_file():
                raise BenchError(f"bundled schema missing: {source}")
            shutil.copyfile(source, bench / "schema" / name)
            copied.append(name)
        update_manifest_timestamp(bench)
    print(json.dumps({"action": "schema-synced", "files": copied}))
    return 0


def selected_cases_for_args(bench: Path, args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = []
    for _, case in iter_cases(bench):
        if getattr(args, "project", None) and case.get("project", {}).get("id") != args.project:
            continue
        if getattr(args, "id", None) and case.get("id") != args.id:
            continue
        if getattr(args, "tag", None) and args.tag not in case.get("tags", []):
            continue
        selected.append(case)
    return selected


def copy_bench_relative(source_bench: Path, destination_bench: Path, relative: str) -> None:
    source = safe_child(source_bench, relative)
    destination = safe_child(destination_bench, relative)
    if not source.is_file():
        raise BenchError(f"export source does not exist: {relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def catalog_case(case: dict[str, Any]) -> dict[str, Any]:
    project_hash = hash_value(case.get("project", {}).get("id", "project"))[:8]
    case_hash = hash_value(case.get("id", "case"))[:12]
    task = case.get("task", {})
    return {
        "id": f"case-{case_hash}",
        "title": case.get("title", "Untitled case"),
        "tags": case.get("tags", []),
        "project": {"id": f"project-{project_hash}", "name": f"Project {project_hash}"},
        "source": {
            "privacy": case.get("source", {}).get("privacy"),
            "summary": case.get("source", {}).get("summary", ""),
        },
        "task": {
            "kind": task.get("kind", "response"),
            "prompt": task.get("prompt", ""),
            "context": task.get("context", {}),
            "constraints": task.get("constraints", []),
            "environment": task.get("environment", {}),
        },
        "difficulty": {
            "signals": case.get("difficulty", {}).get("signals", []),
            "summary": case.get("difficulty", {}).get("summary", ""),
            "key_insight": case.get("difficulty", {}).get("key_insight", ""),
        },
        "runnable": False,
    }


def command_export(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    manifest = load_json(bench / "bench.json")
    manifest_errors = validate_manifest(manifest)
    if manifest_errors:
        raise BenchError("manifest validation failed: " + "; ".join(manifest_errors))
    selected = selected_cases_for_args(bench, args)
    if not selected:
        raise BenchError("no cases matched the export selection")
    for case in selected:
        errors = validate_case(case, bench)
        errors.extend(validate_privacy_policy(case, manifest))
        if errors:
            raise BenchError(f"case {case.get('id')} cannot be exported:\n- " + "\n- ".join(errors))
    include_workspaces = bool(args.include_workspaces)
    if include_workspaces:
        privacy = manifest.get("privacy", {})
        if privacy.get("allow_source_export") is not True:
            raise BenchError("source export is disabled by privacy.allow_source_export")
        if not args.acknowledge_source_disclosure:
            raise BenchError("--include-workspaces requires --acknowledge-source-disclosure")
        disallowed = [case["id"] for case in selected if case.get("source", {}).get("privacy") == "redacted"]
        if disallowed:
            raise BenchError("workspace export requires synthetic or explicitly approved cases: " + ", ".join(disallowed))
        repo_refs = [
            case["id"]
            for case in selected
            if case.get("task", {}).get("workspace", {}).get("mode") == "repo-ref"
        ]
        if repo_refs:
            raise BenchError("portable workspace export requires snapshot cases; repo-ref cases: " + ", ".join(repo_refs))
    elif not args.include_redacted:
        selected = [case for case in selected if case.get("source", {}).get("privacy") == "synthetic"]
        if not selected:
            raise BenchError("catalog export defaults to synthetic cases; pass --include-redacted after reviewing content")
    destination = Path(args.output).expanduser().resolve()
    if destination.exists():
        raise BenchError(f"export destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        if include_workspaces:
            export_manifest = copy.deepcopy(manifest)
            export_manifest["id"] = slugify(manifest["id"] + "-export", fallback="bench-export")
            export_manifest["name"] = manifest["name"] + " Export"
            export_manifest["created_at"] = now_utc()
            export_manifest["updated_at"] = now_utc()
            export_manifest["privacy"] = {"visibility": "team", "allow_source_export": False}
            atomic_json(temporary / "bench.json", export_manifest)
            for schema_name in ("bench-manifest.schema.json", "bench-case.schema.json"):
                copy_bench_relative(bench, temporary, f"schema/{schema_name}")
            for case in selected:
                project_id = case["project"]["id"]
                copy_bench_relative(bench, temporary, f"projects/{project_id}.json")
                atomic_json(temporary / "cases" / project_id / f"{case['id']}.json", case)
                workspace = case.get("task", {}).get("workspace", {})
                if workspace.get("mode") == "snapshot":
                    copy_bench_relative(bench, temporary, workspace["archive"])
                for fixture in case.get("task", {}).get("fixtures", []):
                    copy_bench_relative(bench, temporary, fixture["path"])
                for fixture in case.get("evaluation", {}).get("fixtures", []):
                    copy_bench_relative(bench, temporary, fixture["source"])
            (temporary / "local").mkdir(parents=True, exist_ok=True)
            (temporary / "reports").mkdir(parents=True, exist_ok=True)
            (temporary / "checkpoints").mkdir(parents=True, exist_ok=True)
            (temporary / ".gitignore").write_text(
                "reports/\nlocal/\ncheckpoints/\n",
                encoding="utf-8",
                newline="\n",
            )
            export_kind = "runnable"
        else:
            atomic_json(
                temporary / "catalog.json",
                {
                    "schema_version": CATALOG_VERSION,
                    "bench": {"id": manifest["id"], "name": manifest["name"]},
                    "created_at": now_utc(),
                    "privacy": "metadata-only; workspace, evaluator commands, and root causes omitted",
                    "cases": [catalog_case(case) for case in selected],
                },
            )
            export_kind = "catalog"
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    print(json.dumps({"action": "exported", "kind": export_kind, "output": str(destination), "cases": len(selected)}))
    return 0


def json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise BenchError("JSON Pointer must be empty or start with '/'")
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise BenchError(f"JSON Pointer index not found: {token}") from exc
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise BenchError(f"JSON Pointer key not found: {token}")
    return current


def extract_tar_safely(archive: tarfile.TarFile, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for member in archive.getmembers():
        normalized = member.name.replace("\\", "/")
        path_parts = Path(normalized).parts
        if not normalized or normalized.startswith("/") or ".." in path_parts:
            raise BenchError(f"unsafe archive member: {member.name}")
        target = safe_child(destination, normalized)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            raise BenchError(f"unsupported archive member type: {member.name}")
        source = archive.extractfile(member)
        if source is None:
            raise BenchError(f"cannot read archive member: {member.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            shutil.copyfileobj(source, output)
        try:
            target.chmod(member.mode)
        except OSError:
            pass


def copy_selected_files(files: list[dict[str, Any]], destination: Path) -> None:
    for item in files:
        target = safe_child(destination, item["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item["data"])
        try:
            target.chmod(item["mode"])
        except OSError:
            pass


def verify_snapshot(project_root: Path, descriptor: dict[str, Any], manifest: dict[str, Any]) -> None:
    if hash_value(manifest) != descriptor["content_sha256"]:
        raise BenchError("workspace snapshot manifest does not match content_sha256")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != descriptor["file_count"]:
        raise BenchError("workspace snapshot file count does not match descriptor")
    expected_paths = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BenchError("workspace snapshot manifest contains an invalid file entry")
        expected_paths.add(item["path"])
        path = safe_child(project_root, item["path"])
        if not path.is_file() or path.stat().st_size != item.get("bytes") or hash_file(path) != item.get("sha256"):
            raise BenchError(f"workspace snapshot file failed verification: {item['path']}")
    actual_paths = {
        path.relative_to(project_root).as_posix()
        for path in project_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_paths != expected_paths:
        raise BenchError("workspace snapshot contains files not declared by its manifest")


def materialize_snapshot(descriptor: dict[str, Any], bench: Path, temporary_root: Path) -> Path:
    archive_path = safe_child(bench, descriptor["archive"])
    if hash_file(archive_path) != descriptor["sha256"]:
        raise BenchError("workspace archive hash changed since capture")
    with tarfile.open(archive_path, mode="r:gz") as archive:
        extract_tar_safely(archive, temporary_root)
    manifest_path = temporary_root / ".bench-snapshot.json"
    manifest = load_json(manifest_path)
    project_root = temporary_root / descriptor["root"]
    if not project_root.is_dir():
        raise BenchError("workspace snapshot is missing its project root")
    verify_snapshot(project_root, descriptor, manifest)
    manifest_path.unlink()
    return project_root


def materialize_repo_ref(
    descriptor: dict[str, Any], bench: Path, project_id: str, temporary_root: Path, max_total_mb: int
) -> Path:
    source = resolve_registered_project(bench, project_id)
    process = git_command(source, ["archive", "--format=tar", descriptor["base_commit"]])
    if process is None or process.returncode != 0:
        message = "git archive failed"
        if process is not None:
            message = process.stderr.decode("utf-8", errors="replace").strip() or message
        raise BenchError(f"cannot restore repo-ref base_commit: {message}")
    raw_root = temporary_root / "git-archive"
    with tarfile.open(fileobj=io.BytesIO(process.stdout), mode="r:") as archive:
        extract_tar_safely(archive, raw_root)
    files = select_workspace_files(
        raw_root,
        max_total_mb=max(max_total_mb, 1),
        max_file_mb=max(max_total_mb, 1),
    )
    content_manifest = workspace_content_manifest(files)
    actual_content_sha256 = hash_value(content_manifest)
    if actual_content_sha256 != descriptor["content_sha256"]:
        raise BenchError(
            "repo-ref base_commit no longer materializes to the captured input state "
            f"(expected {descriptor['content_sha256']}, got {actual_content_sha256})"
        )
    project_root = temporary_root / "project"
    copy_selected_files(files, project_root)
    return project_root


def copy_case_fixtures(case: dict[str, Any], bench: Path, candidate_bench: Path) -> None:
    for fixture in case.get("task", {}).get("fixtures", []):
        source = safe_child(bench, fixture["path"])
        destination = safe_child(candidate_bench, fixture["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def copy_evaluator_fixtures(case: dict[str, Any], bench: Path, workspace_root: Path) -> None:
    for fixture in case.get("evaluation", {}).get("fixtures", []):
        source = safe_child(bench, fixture["source"])
        destination = safe_child(workspace_root, fixture["target"])
        if destination.exists():
            raise BenchError(f"evaluator fixture would overwrite candidate-visible file: {fixture['target']}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def tree_state(root: Path) -> dict[str, str]:
    state: dict[str, str] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if name not in DEFAULT_EXCLUDED_PARTS]
        for filename in filenames:
            path = current_path / filename
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                state[relative] = "symlink:" + os.readlink(path)
            elif path.is_file():
                state[relative] = hash_file(path)
    return state


def changed_tree_paths(before: dict[str, str], after: dict[str, str]) -> list[dict[str, str]]:
    changes = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changes.append({"path": path, "change": "added"})
        elif path not in after:
            changes.append({"path": path, "change": "deleted"})
        elif before[path] != after[path]:
            changes.append({"path": path, "change": "modified"})
    return changes


def current_platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def prepare_case_environment(
    case: dict[str, Any], workspace_root: Path
) -> tuple[dict[str, str], list[dict[str, Any]], str | None]:
    environment = case.get("task", {}).get("environment", {})
    platforms = environment.get("platforms", ["any"])
    current = current_platform_name()
    if "any" not in platforms and current not in platforms:
        return dict(os.environ), [], f"case requires platform(s) {platforms}; runner is {current}"
    process_environment = dict(os.environ)
    process_environment.update(environment.get("variables", {}))
    process_environment["AI_WORK_BENCH_WORKSPACE"] = str(workspace_root)
    step_results: list[dict[str, Any]] = []
    for phase in ("preflight", "setup"):
        for step in environment.get(phase, []):
            timeout = step.get("timeout_seconds", 300)
            started = now_utc()
            try:
                process = subprocess.run(
                    step["command"],
                    cwd=workspace_root,
                    env=process_environment,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    shell=False,
                    check=False,
                )
                result = {
                    "id": step["id"],
                    "phase": phase,
                    "status": "passed" if process.returncode == step.get("expected_exit_code", 0) else "failed",
                    "exit_code": process.returncode,
                    "expected_exit_code": step.get("expected_exit_code", 0),
                    "stdout": process.stdout[-4000:],
                    "stderr": process.stderr[-4000:],
                    "started_at": started,
                    "finished_at": now_utc(),
                }
            except subprocess.TimeoutExpired as exc:
                result = {
                    "id": step["id"],
                    "phase": phase,
                    "status": "failed",
                    "timed_out": True,
                    "stdout": exc.stdout[-4000:] if isinstance(exc.stdout, str) else "",
                    "stderr": exc.stderr[-4000:] if isinstance(exc.stderr, str) else "",
                    "started_at": started,
                    "finished_at": now_utc(),
                }
            except OSError as exc:
                result = {
                    "id": step["id"],
                    "phase": phase,
                    "status": "failed",
                    "error": str(exc),
                    "started_at": started,
                    "finished_at": now_utc(),
                }
            step_results.append(result)
            if result["status"] != "passed":
                return process_environment, step_results, f"{phase} step {step['id']!r} failed"
    return process_environment, step_results, None


def evaluate_check(
    check: dict[str, Any],
    result: dict[str, Any],
    workspace_root: Path,
    changed_paths: list[dict[str, str]],
    process_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    check_type = check["type"]
    outcome: dict[str, Any] = {"id": check["id"], "type": check_type}
    if check_type == "manual":
        outcome.update({"status": "pending", "rubric": check["rubric"]})
        return outcome
    outcome["weight"] = float(check.get("weight", 1))
    text = result.get("text", "")
    passed = False
    detail = ""
    try:
        if check_type == "text_contains":
            passed = check["expected"] in text
            detail = "expected substring found" if passed else "expected substring not found"
        elif check_type == "text_not_contains":
            passed = check["expected"] not in text
            detail = "forbidden substring absent" if passed else "forbidden substring found"
        elif check_type == "text_regex":
            passed = re.search(check["pattern"], text) is not None
            detail = "pattern matched" if passed else "pattern did not match"
        elif check_type == "json_pointer_equals":
            actual = json_pointer(result.get("data"), check["pointer"])
            passed = actual == check.get("expected")
            detail = "value matched" if passed else f"value differed: {actual!r}"
        elif check_type in {"artifact_exists", "artifact_sha256"}:
            artifact = safe_child(workspace_root, check["path"])
            if check_type == "artifact_exists":
                passed = artifact.exists()
                detail = "artifact exists" if passed else "artifact missing"
            else:
                passed = artifact.is_file() and hash_file(artifact) == check["expected"]
                detail = "artifact hash matched" if passed else "artifact missing or hash differed"
        elif check_type == "command":
            expected_exit = check.get("expected_exit_code", 0)
            timeout = check.get("timeout_seconds", 300)
            try:
                process = subprocess.run(
                    check["command"],
                    cwd=workspace_root,
                    env=process_environment,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    shell=False,
                    check=False,
                )
                passed = process.returncode == expected_exit
                detail = f"exit code {process.returncode}; expected {expected_exit}"
                outcome["stdout"] = process.stdout[-4000:]
                outcome["stderr"] = process.stderr[-4000:]
            except subprocess.TimeoutExpired as exc:
                passed = False
                detail = f"command timed out after {timeout} seconds"
                outcome["stdout"] = exc.stdout[-4000:] if isinstance(exc.stdout, str) else ""
                outcome["stderr"] = exc.stderr[-4000:] if isinstance(exc.stderr, str) else ""
        elif check_type == "changed_paths":
            paths = [item["path"] for item in changed_paths]
            allow = check.get("allow", [])
            require = check.get("require", [])
            forbidden = [path for path in paths if allow and not any(fnmatch.fnmatch(path, pattern) for pattern in allow)]
            missing = [pattern for pattern in require if not any(fnmatch.fnmatch(path, pattern) for path in paths)]
            passed = not forbidden and not missing
            detail = f"{len(paths)} changed path(s)"
            if forbidden:
                detail += f"; outside allowlist: {', '.join(forbidden[:20])}"
            if missing:
                detail += f"; required changes missing: {', '.join(missing)}"
            outcome["changed_paths"] = changed_paths
    except (BenchError, OSError, subprocess.SubprocessError) as exc:
        passed = False
        detail = str(exc)
    outcome.update({"status": "passed" if passed else "failed", "detail": detail})
    return outcome


def parse_candidate_output(stdout: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return {"protocol": RESULT_PROTOCOL, "text": stdout, "data": None, "artifacts": [], "metadata": {}}
    if not isinstance(value, dict):
        return {"protocol": RESULT_PROTOCOL, "text": stdout, "data": value, "artifacts": [], "metadata": {}}
    return {
        "protocol": value.get("protocol", RESULT_PROTOCOL),
        "text": value.get("text", "") if isinstance(value.get("text", ""), str) else str(value.get("text")),
        "data": value.get("data"),
        "artifacts": value.get("artifacts", []),
        "metadata": value.get("metadata", {}),
    }


def execute_case(
    case: dict[str, Any],
    command: str | list[str],
    workspace_root: Path,
    candidate_bench: Path,
    global_bench: Path,
    timeout: int,
) -> dict[str, Any]:
    bench_relative = Path(os.path.relpath(candidate_bench, workspace_root)).as_posix()
    request = {
        "protocol": CANDIDATE_PROTOCOL,
        "case_id": case["id"],
        "project": case["project"],
        "bench_root": bench_relative,
        "workspace_root": ".",
        "task": case["task"],
    }
    started = now_utc()
    process_environment, environment_steps, environment_error = prepare_case_environment(case, workspace_root)
    if environment_error:
        return infrastructure_result(case, started, environment_error, environment_steps)
    track_changes = any(check.get("type") == "changed_paths" for check in case["evaluation"]["checks"])
    before = tree_state(workspace_root) if track_changes else {}
    try:
        process = subprocess.run(
            command,
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=workspace_root,
            env=process_environment,
            shell=isinstance(command, str),
            timeout=timeout,
            check=False,
        )
        exit_code = process.returncode
        stdout = process.stdout
        stderr = process.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        timed_out = True
    except OSError as exc:
        return infrastructure_result(case, started, f"candidate could not start: {exc}", environment_steps)
    result = parse_candidate_output(stdout)
    changes = changed_tree_paths(before, tree_state(workspace_root)) if track_changes else []
    protocol_ok = result.get("protocol") == RESULT_PROTOCOL
    if exit_code != 0 or timed_out or not protocol_ok:
        return {
            "case_id": case["id"],
            "project_id": case["project"]["id"],
            "revision": case["revision"],
            "status": "failed",
            "score": None,
            "pass_threshold": float(case["evaluation"]["pass_threshold"]),
            "process": {
                "exit_code": exit_code,
                "timed_out": timed_out,
                "protocol_ok": protocol_ok,
                "stderr": stderr[-4000:],
            },
            "checks": [],
            "candidate_metadata": result.get("metadata", {}),
            "changed_paths": changes,
            "environment": {
                "runner_platform": current_platform_name(),
                "runner_python": platform.python_version(),
                "declared": case.get("task", {}).get("environment", {}),
                "steps": environment_steps,
            },
            "started_at": started,
            "finished_at": now_utc(),
        }
    try:
        with tempfile.TemporaryDirectory(prefix=f"awb-eval-{case['id'][:20]}-") as evaluation_temporary:
            evaluation_root = Path(evaluation_temporary) / "project"
            shutil.copytree(workspace_root, evaluation_root, symlinks=True)
            copy_evaluator_fixtures(case, global_bench, evaluation_root)
            evaluation_environment = dict(process_environment)
            evaluation_environment["AI_WORK_BENCH_WORKSPACE"] = str(evaluation_root)
            checks = [
                evaluate_check(check, result, evaluation_root, changes, evaluation_environment)
                for check in case["evaluation"]["checks"]
            ]
    except (BenchError, OSError, shutil.Error) as exc:
        return infrastructure_result(
            case,
            started,
            f"evaluator workspace preparation failed: {exc}",
            environment_steps,
        )
    automated = [check for check in checks if check["status"] != "pending"]
    pending = any(check["status"] == "pending" for check in checks)
    total_weight = sum(check.get("weight", 0.0) for check in automated)
    passed_weight = sum(check.get("weight", 0.0) for check in automated if check["status"] == "passed")
    score = passed_weight / total_weight if total_weight else None
    process_ok = exit_code == 0 and not timed_out and protocol_ok
    threshold = float(case["evaluation"]["pass_threshold"])
    automated_ok = score is not None and score >= threshold
    if not process_ok or (score is not None and not automated_ok):
        status = "failed"
    elif pending or score is None:
        status = "needs_review"
    else:
        status = "passed"
    return {
        "case_id": case["id"],
        "project_id": case["project"]["id"],
        "revision": case["revision"],
        "status": status,
        "score": score,
        "pass_threshold": threshold,
        "process": {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "protocol_ok": protocol_ok,
            "stderr": stderr[-4000:],
        },
        "environment": {
            "runner_platform": current_platform_name(),
            "runner_python": platform.python_version(),
            "declared": case.get("task", {}).get("environment", {}),
            "steps": environment_steps,
        },
        "checks": checks,
        "candidate_metadata": result.get("metadata", {}),
        "changed_paths": changes,
        "started_at": started,
        "finished_at": now_utc(),
    }


def infrastructure_result(
    case: dict[str, Any],
    started: str,
    error: str,
    environment_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case.get("id", "unknown"),
        "project_id": case.get("project", {}).get("id", "unknown"),
        "revision": case.get("revision"),
        "status": "infrastructure_error",
        "score": None,
        "error": error,
        "environment": {
            "runner_platform": current_platform_name(),
            "runner_python": platform.python_version(),
            "steps": environment_steps or [],
        },
        "started_at": started,
        "finished_at": now_utc(),
    }


def run_one(
    case: dict[str, Any], command: str | list[str], bench: Path, timeout: int, max_snapshot_mb: int
) -> dict[str, Any]:
    descriptor = case.get("task", {}).get("workspace")
    with tempfile.TemporaryDirectory(prefix=f"awb-{case['id'][:24]}-") as temporary:
        temporary_root = Path(temporary)
        if descriptor is None:
            materialized_root = temporary_root / "project"
            materialized_root.mkdir(parents=True, exist_ok=True)
        elif descriptor["mode"] == "snapshot":
            materialized_root = materialize_snapshot(descriptor, bench, temporary_root)
        else:
            materialized_root = materialize_repo_ref(
                descriptor,
                bench,
                case["project"]["id"],
                temporary_root,
                max_snapshot_mb,
            )
        subdir = descriptor.get("subdir", ".") if descriptor else "."
        workspace_root = safe_child(materialized_root, subdir)
        if not workspace_root.is_dir():
            raise BenchError(f"workspace subdir does not exist after materialization: {subdir}")
        candidate_bench = temporary_root / ".bench-input"
        copy_case_fixtures(case, bench, candidate_bench)
        return execute_case(case, command, workspace_root, candidate_bench, bench, timeout)


def run_one_safe(
    case: dict[str, Any], command: str | list[str], bench: Path, timeout: int, max_snapshot_mb: int
) -> dict[str, Any]:
    started = now_utc()
    try:
        return run_one(case, command, bench, timeout, max_snapshot_mb)
    except (BenchError, OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        return infrastructure_result(case, started, str(exc))


def command_run(args: argparse.Namespace) -> int:
    bench = resolve_bench(args.bench)
    manifest = load_json(bench / "bench.json")
    manifest_errors = validate_manifest(manifest)
    if manifest_errors:
        raise BenchError("manifest validation failed: " + "; ".join(manifest_errors))
    selected: list[dict[str, Any]] = []
    for _, case in iter_cases(bench):
        if args.project and case.get("project", {}).get("id") != args.project:
            continue
        if args.id and case.get("id") != args.id:
            continue
        if args.tag and args.tag not in case.get("tags", []):
            continue
        if not args.include_draft and case.get("status") != "ready":
            continue
        if case.get("status") == "retired":
            continue
        errors = validate_case(case, bench, ready_strict=True)
        errors.extend(validate_privacy_policy(case, manifest))
        errors.extend(validate_capture_policy(case, manifest, bench, exclude_case_id=case.get("id")))
        if errors:
            raise BenchError(f"case {case.get('id')} is not runnable:\n- " + "\n- ".join(errors))
        selected.append(case)
    if not selected:
        raise BenchError("no runnable cases matched the selection")
    timeout = args.timeout or int(manifest["runner"]["timeout_seconds"])
    max_snapshot_mb = int(manifest["capture_policy"].get("max_snapshot_mb", 100))
    if getattr(args, "candidate_json", None):
        try:
            candidate_command = json.loads(args.candidate_json)
        except json.JSONDecodeError as exc:
            raise BenchError(f"--candidate-json must be valid JSON: {exc}") from exc
        if not isinstance(candidate_command, list) or not candidate_command or any(
            not isinstance(item, str) or not item for item in candidate_command
        ):
            raise BenchError("--candidate-json must be a non-empty JSON array of strings")
    else:
        candidate_command = args.candidate
    report = {
        "schema_version": REPORT_VERSION,
        "bench_id": manifest["id"],
        "candidate_command": candidate_command,
        "created_at": now_utc(),
        "results": [
            run_one_safe(case, candidate_command, bench, timeout, max_snapshot_mb) for case in selected
        ],
    }
    report["summary"] = {
        "total": len(report["results"]),
        "passed": sum(item["status"] == "passed" for item in report["results"]),
        "failed": sum(item["status"] == "failed" for item in report["results"]),
        "needs_review": sum(item["status"] == "needs_review" for item in report["results"]),
        "infrastructure_errors": sum(item["status"] == "infrastructure_error" for item in report["results"]),
    }
    if args.report:
        report_path = Path(args.report).expanduser()
        if not report_path.is_absolute():
            report_path = Path.cwd() / report_path
    else:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = bench / "reports" / f"run-{stamp}-{uuid.uuid4().hex[:8]}.json"
    atomic_json(report_path.resolve(), report)
    print(json.dumps({"report": str(report_path.resolve()), **report["summary"]}, ensure_ascii=False))
    if report["summary"]["failed"] or report["summary"]["infrastructure_errors"]:
        return 1
    if report["summary"]["needs_review"]:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchctl", description="Manage a global ai-work-bench/v1 hub")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_bench_argument(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--bench",
            help="global bench directory; defaults to AI_WORK_BENCH_HOME or $CODEX_HOME/benches/daily-work",
        )

    init_parser = subparsers.add_parser("init", help="initialize a global bench hub")
    add_bench_argument(init_parser)
    init_parser.add_argument("--name")
    init_parser.add_argument("--id")
    init_parser.add_argument("--description")
    init_parser.add_argument("--mode", choices=("auto", "suggest", "off"), default="suggest")
    init_parser.add_argument("--minimum-signals", type=int, default=2)
    init_parser.add_argument("--max-cases-per-task", type=int, default=1)
    init_parser.add_argument("--workspace-mode", choices=("off", "repo-ref", "snapshot"), default="snapshot")
    init_parser.add_argument("--max-snapshot-mb", type=int, default=100)
    init_parser.add_argument("--timeout", type=int, default=120)
    init_parser.add_argument("--visibility", choices=("private", "team"), default="private")
    init_parser.add_argument("--allow-source-export", action="store_true")
    init_parser.set_defaults(func=command_init)

    capture_parser = subparsers.add_parser("capture", help="add or revise a case")
    add_bench_argument(capture_parser)
    capture_parser.add_argument("--input", required=True, help="path to a case JSON draft")
    capture_parser.add_argument("--checkpoint", help="active before-task checkpoint to attach as task.workspace")
    capture_parser.add_argument("--promote", action="store_true", help="capture directly as ready after strict validation")
    capture_parser.add_argument("--confirmed", action="store_true", help="record user confirmation for suggest mode")
    capture_parser.add_argument("--force-policy", action="store_true", help="override signal/count policy, not privacy checks")
    capture_parser.set_defaults(func=command_capture)

    project_parser = subparsers.add_parser("project", help="manage source repositories in the global hub")
    project_subparsers = project_parser.add_subparsers(dest="project_command", required=True)

    project_register = project_subparsers.add_parser("register", help="register or refresh a local repository")
    add_bench_argument(project_register)
    project_register.add_argument("--source", default=".")
    project_register.add_argument("--id")
    project_register.add_argument("--name")
    project_register.set_defaults(func=command_project_register)

    project_list = project_subparsers.add_parser("list", help="list registered repositories")
    add_bench_argument(project_list)
    project_list.set_defaults(func=command_project_list)

    checkpoint_parser = subparsers.add_parser("checkpoint", help="capture before-task engineering workspace state")
    checkpoint_subparsers = checkpoint_parser.add_subparsers(dest="checkpoint_command", required=True)

    checkpoint_start = checkpoint_subparsers.add_parser("start", help="capture state before the first task mutation")
    add_bench_argument(checkpoint_start)
    checkpoint_start.add_argument("--source", default=".")
    checkpoint_start.add_argument("--project-id")
    checkpoint_start.add_argument("--project-name")
    checkpoint_start.add_argument("--mode", choices=("repo-ref", "snapshot"))
    checkpoint_start.add_argument("--max-mb", type=int)
    checkpoint_start.add_argument("--max-file-mb", type=int, default=20)
    checkpoint_start.add_argument("--trigger", choices=("auto", "suggest", "explicit"), default="explicit")
    checkpoint_start.add_argument("--task-id")
    checkpoint_start.add_argument("--force-policy", action="store_true")
    checkpoint_start.set_defaults(func=command_checkpoint_start)

    checkpoint_list = checkpoint_subparsers.add_parser("list", help="list captured checkpoints")
    add_bench_argument(checkpoint_list)
    checkpoint_list.add_argument("--status", choices=("active", "captured", "discarded"))
    checkpoint_list.set_defaults(func=command_checkpoint_list)

    checkpoint_discard = checkpoint_subparsers.add_parser("discard", help="mark an unused checkpoint as discarded")
    add_bench_argument(checkpoint_discard)
    checkpoint_discard.add_argument("--id", required=True)
    checkpoint_discard.set_defaults(func=command_checkpoint_discard)

    validate_parser = subparsers.add_parser("validate", help="validate manifest, cases, fixtures, and copied schemas")
    add_bench_argument(validate_parser)
    validate_parser.add_argument("--id")
    validate_parser.add_argument("--project")
    validate_parser.set_defaults(func=command_validate)

    list_parser = subparsers.add_parser("list", help="list cases")
    add_bench_argument(list_parser)
    list_parser.add_argument("--status", choices=("draft", "ready", "retired"))
    list_parser.add_argument("--project")
    list_parser.set_defaults(func=command_list)

    promote_parser = subparsers.add_parser("promote", help="promote a validated case to ready")
    add_bench_argument(promote_parser)
    promote_parser.add_argument("--id", required=True)
    promote_parser.add_argument("--force-policy", action="store_true")
    promote_parser.set_defaults(func=command_promote)

    policy_parser = subparsers.add_parser("policy", help="inspect capture policy or make a phase decision")
    policy_subparsers = policy_parser.add_subparsers(dest="policy_command", required=True)
    policy_show = policy_subparsers.add_parser("show")
    add_bench_argument(policy_show)
    policy_show.set_defaults(func=command_policy_show)
    policy_decide = policy_subparsers.add_parser("decide")
    add_bench_argument(policy_decide)
    policy_decide.add_argument("--phase", choices=("before", "after"), required=True)
    policy_decide.add_argument("--signal", action="append")
    policy_decide.add_argument("--explicit", action="store_true")
    policy_decide.set_defaults(func=command_policy_decide)

    schema_parser = subparsers.add_parser("schema", help="manage copied authoritative schemas")
    schema_subparsers = schema_parser.add_subparsers(dest="schema_command", required=True)
    schema_sync = schema_subparsers.add_parser("sync")
    add_bench_argument(schema_sync)
    schema_sync.set_defaults(func=command_schema_sync)

    export_parser = subparsers.add_parser("export", help="export a sanitized catalog or explicitly approved runnable cases")
    add_bench_argument(export_parser)
    export_parser.add_argument("--output", required=True)
    export_parser.add_argument("--id")
    export_parser.add_argument("--project")
    export_parser.add_argument("--tag")
    export_parser.add_argument("--include-redacted", action="store_true")
    export_parser.add_argument("--include-workspaces", action="store_true")
    export_parser.add_argument("--acknowledge-source-disclosure", action="store_true")
    export_parser.set_defaults(func=command_export)

    run_parser = subparsers.add_parser("run", help="run selected cases against a candidate command")
    add_bench_argument(run_parser)
    candidate_group = run_parser.add_mutually_exclusive_group(required=True)
    candidate_group.add_argument("--candidate", help="trusted shell command; prefer --candidate-json")
    candidate_group.add_argument("--candidate-json", help="trusted command as a JSON argument array")
    run_parser.add_argument("--id")
    run_parser.add_argument("--project")
    run_parser.add_argument("--tag")
    run_parser.add_argument("--include-draft", action="store_true")
    run_parser.add_argument("--timeout", type=int)
    run_parser.add_argument("--report")
    run_parser.set_defaults(func=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

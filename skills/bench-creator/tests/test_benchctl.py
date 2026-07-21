from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("benchctl", SKILL_ROOT / "scripts" / "benchctl.py")
assert SPEC and SPEC.loader
benchctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchctl)


class BenchctlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="benchctl-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def init_bench(
        self,
        *,
        mode: str = "suggest",
        minimum_signals: int = 2,
        max_cases_per_task: int = 1,
        allow_source_export: bool = False,
    ) -> Path:
        bench = self.root / ("bench-" + mode)
        with redirect_stdout(io.StringIO()):
            benchctl.command_init(
                SimpleNamespace(
                    bench=str(bench),
                    name="Test Bench",
                    id="test-bench",
                    description=None,
                    mode=mode,
                    minimum_signals=minimum_signals,
                    max_cases_per_task=max_cases_per_task,
                    workspace_mode="snapshot",
                    max_snapshot_mb=10,
                    timeout=30,
                    visibility="private",
                    allow_source_export=allow_source_export,
                )
            )
        return bench

    def case(
        self,
        case_id: str,
        *,
        privacy: str = "synthetic",
        trigger: str = "explicit",
        task_id: str | None = None,
        signals: list[str] | None = None,
        platforms: list[str] | None = None,
    ) -> dict:
        source = {
            "kind": "manual",
            "dedupe_key": "tests/" + case_id,
            "privacy": privacy,
            "summary": "Synthetic test case.",
            "capture_trigger": trigger,
        }
        if task_id:
            source["capture_task_id"] = task_id
        return {
            "schema_version": "ai-work-bench/case-v1",
            "id": case_id,
            "status": "ready",
            "title": "Synthetic test case",
            "tags": ["test"],
            "project": {"id": "test-project", "name": "Test Project"},
            "source": source,
            "task": {
                "kind": "response",
                "prompt": "hello bench",
                "context": {},
                "constraints": [],
                "fixtures": [],
                "environment": {
                    "platforms": platforms or ["any"],
                    "runtime": {"python": ">=3.10"},
                    "network": "off",
                    "variables": {},
                    "services": [],
                    "preflight": [],
                    "setup": [],
                },
            },
            "difficulty": {
                "signals": signals or ["verification-gap", "non-obvious-root-cause"],
                "summary": "Synthetic difficulty.",
                "root_cause": "Synthetic root cause.",
                "failed_approaches": [],
                "key_insight": "Exercise the complete protocol.",
            },
            "evaluation": {
                "pass_threshold": 1.0,
                "fixtures": [],
                "checks": [{"id": "echo", "type": "text_contains", "expected": "hello bench"}],
            },
        }

    def write_case(self, value: dict, name: str = "case.json") -> Path:
        path = self.root / name
        benchctl.atomic_json(path, value)
        return path

    def capture(self, bench: Path, value: dict, **overrides) -> int:
        path = self.write_case(value, overrides.pop("name", "case.json"))
        options = {
            "bench": str(bench),
            "input": str(path),
            "checkpoint": None,
            "promote": True,
            "confirmed": False,
            "force_policy": False,
        }
        options.update(overrides)
        with redirect_stdout(io.StringIO()):
            return benchctl.command_capture(SimpleNamespace(**options))

    def run_args(self, bench: Path, report: Path, candidate: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            bench=str(bench),
            candidate=None,
            candidate_json=json.dumps(candidate),
            id=None,
            project=None,
            tag=None,
            include_draft=False,
            timeout=30,
            report=str(report),
        )

    def test_response_smoke_runs_setup_and_candidate(self) -> None:
        bench = self.init_bench()
        case = self.case("response-smoke")
        case["task"]["environment"]["setup"] = [
            {
                "id": "prepare",
                "command": [sys.executable, "-c", "from pathlib import Path; Path('prepared.txt').write_text('ok')"],
            }
        ]
        case["evaluation"]["checks"].append(
            {"id": "prepared", "type": "artifact_exists", "path": "prepared.txt"}
        )
        self.capture(bench, case)
        report = self.root / "report.json"
        candidate = [sys.executable, str(SKILL_ROOT / "scripts" / "example_candidate.py")]
        with redirect_stdout(io.StringIO()):
            result = benchctl.command_run(self.run_args(bench, report, candidate))
        self.assertEqual(result, 0)
        value = benchctl.load_json(report)
        self.assertEqual(value["summary"]["passed"], 1)
        self.assertEqual(value["results"][0]["environment"]["steps"][0]["status"], "passed")

    def test_portable_launcher_resolves_skill_relative_cli(self) -> None:
        initialized = self.root / "launcher-bench"
        if sys.platform.startswith("win"):
            command = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SKILL_ROOT / "scripts" / "benchctl.ps1"),
            ]
        else:
            command = ["sh", str(SKILL_ROOT / "scripts" / "benchctl.sh")]
        command.extend(["init", "--bench", str(initialized), "--name", "Launcher Bench"])
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("initialized ai-work-bench/v1", result.stdout)
        self.assertTrue((initialized / "bench.json").is_file())

    def test_auto_policy_enforces_minimum_signals(self) -> None:
        bench = self.init_bench(mode="auto", minimum_signals=2)
        case = self.case(
            "auto-too-easy",
            trigger="auto",
            task_id="task-policy-test",
            signals=["verification-gap"],
        )
        with self.assertRaisesRegex(benchctl.BenchError, "at least 2 difficulty signal"):
            self.capture(bench, case)

    def test_suggest_policy_requires_confirmation(self) -> None:
        bench = self.init_bench(mode="suggest")
        case = self.case("suggest-case", trigger="suggest", task_id="task-suggest-test")
        with self.assertRaisesRegex(benchctl.BenchError, "requires explicit user confirmation"):
            self.capture(bench, case)
        case["source"]["user_confirmed"] = True
        self.capture(bench, case)

    def test_auto_policy_enforces_max_cases_per_task(self) -> None:
        bench = self.init_bench(mode="auto", max_cases_per_task=1)
        self.capture(bench, self.case("first-auto", trigger="auto", task_id="task-one-case"), name="first.json")
        with self.assertRaisesRegex(benchctl.BenchError, "at most 1 case"):
            self.capture(
                bench,
                self.case("second-auto", trigger="auto", task_id="task-one-case"),
                name="second.json",
            )

    def test_snapshot_hidden_evaluator_and_changed_paths(self) -> None:
        bench = self.init_bench(mode="auto")
        source = self.root / "source"
        source.mkdir()
        (source / "app.txt").write_text("before", encoding="utf-8")
        checkpoint_args = SimpleNamespace(
            bench=str(bench),
            source=str(source),
            project_id="snapshot-project",
            project_name="Snapshot Project",
            mode="snapshot",
            max_mb=10,
            max_file_mb=2,
            trigger="auto",
            task_id="task-snapshot-test",
            force_policy=False,
        )
        with redirect_stdout(io.StringIO()):
            benchctl.command_checkpoint_start(checkpoint_args)
        checkpoint_path = next((bench / "checkpoints").glob("*.json"))
        checkpoint = benchctl.load_json(checkpoint_path)
        oracle_relative = "oracles/snapshot-project/modify-file-case/hidden_test.py"
        oracle = bench / oracle_relative
        oracle.parent.mkdir(parents=True)
        oracle.write_text(
            "from pathlib import Path\nassert Path('app.txt').read_text(encoding='utf-8') == 'after'\n",
            encoding="utf-8",
        )
        case = self.case("modify-file-case")
        case["project"] = {"id": "snapshot-project", "name": "Snapshot Project"}
        case["task"] = {
            "kind": "repo",
            "prompt": "Change app.txt to after.",
            "context": {},
            "constraints": [],
            "fixtures": [],
            "environment": case["task"]["environment"],
        }
        case["evaluation"] = {
            "pass_threshold": 1.0,
            "fixtures": [
                {
                    "source": oracle_relative,
                    "target": "tests/.bench_hidden/hidden_test.py",
                    "sha256": hashlib.sha256(oracle.read_bytes()).hexdigest(),
                }
            ],
            "checks": [
                {
                    "id": "hidden-test",
                    "type": "command",
                    "command": [sys.executable, "tests/.bench_hidden/hidden_test.py"],
                    "expected_exit_code": 0,
                },
                {
                    "id": "scoped-diff",
                    "type": "changed_paths",
                    "allow": ["app.txt"],
                    "require": ["app.txt"],
                },
            ],
        }
        self.capture(bench, case, checkpoint=checkpoint["id"])
        candidate = self.root / "candidate.py"
        candidate.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "json.load(sys.stdin)\n"
            "assert not Path('tests/.bench_hidden/hidden_test.py').exists()\n"
            "Path('app.txt').write_text('after', encoding='utf-8')\n"
            "json.dump({'protocol':'ai-work-bench/result-v1','text':'done'}, sys.stdout)\n",
            encoding="utf-8",
        )
        report = self.root / "snapshot-report.json"
        with redirect_stdout(io.StringIO()):
            result = benchctl.command_run(self.run_args(bench, report, [sys.executable, str(candidate)]))
        self.assertEqual(result, 0)
        self.assertEqual(benchctl.load_json(report)["summary"]["passed"], 1)

    def test_parallel_checkpoints_have_unique_ids(self) -> None:
        bench = self.init_bench(mode="auto")
        source = self.root / "parallel-source"
        source.mkdir()
        (source / "input.txt").write_text("same input", encoding="utf-8")

        def checkpoint(index: int) -> int:
            args = SimpleNamespace(
                bench=str(bench),
                source=str(source),
                project_id="parallel-project",
                project_name="Parallel Project",
                mode="snapshot",
                max_mb=10,
                max_file_mb=2,
                trigger="auto",
                task_id=f"task-parallel-{index}",
                force_policy=False,
            )
            return benchctl.command_checkpoint_start(args)

        with redirect_stdout(io.StringIO()):
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(checkpoint, range(4))), [0, 0, 0, 0])
        checkpoints = [benchctl.load_json(path) for path in (bench / "checkpoints").glob("*.json")]
        self.assertEqual(len(checkpoints), 4)
        self.assertEqual(len({item["id"] for item in checkpoints}), 4)

    def test_catalog_export_is_sanitized_and_source_export_is_gated(self) -> None:
        bench = self.init_bench()
        self.capture(bench, self.case("export-case"))
        catalog = self.root / "catalog-export"
        args = SimpleNamespace(
            bench=str(bench),
            output=str(catalog),
            id=None,
            project=None,
            tag=None,
            include_redacted=False,
            include_workspaces=False,
            acknowledge_source_disclosure=False,
        )
        with redirect_stdout(io.StringIO()):
            benchctl.command_export(args)
        value = benchctl.load_json(catalog / "catalog.json")
        self.assertFalse(value["cases"][0]["runnable"])
        self.assertNotIn("evaluation", value["cases"][0])
        args.output = str(self.root / "runnable-export")
        args.include_workspaces = True
        args.acknowledge_source_disclosure = True
        with self.assertRaisesRegex(benchctl.BenchError, "source export is disabled"):
            benchctl.command_export(args)

    def test_run_continues_after_infrastructure_error(self) -> None:
        bench = self.init_bench()
        unavailable = "darwin" if benchctl.current_platform_name() != "darwin" else "windows"
        self.capture(bench, self.case("will-pass"), name="pass.json")
        self.capture(bench, self.case("wrong-platform", platforms=[unavailable]), name="wrong.json")
        report = self.root / "mixed-report.json"
        candidate = [sys.executable, str(SKILL_ROOT / "scripts" / "example_candidate.py")]
        with redirect_stdout(io.StringIO()):
            result = benchctl.command_run(self.run_args(bench, report, candidate))
        self.assertEqual(result, 1)
        summary = benchctl.load_json(report)["summary"]
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["infrastructure_errors"], 1)

    def test_validate_detects_schema_drift(self) -> None:
        bench = self.init_bench()
        schema = bench / "schema" / "bench-case.schema.json"
        value = benchctl.load_json(schema)
        value["description"] = "drift"
        benchctl.atomic_json(schema, value)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = benchctl.command_validate(SimpleNamespace(bench=str(bench), id=None, project=None))
        self.assertEqual(result, 1)

    def test_write_ahead_journal_recovers_on_next_lock(self) -> None:
        bench = self.init_bench()
        manifest = benchctl.load_json(bench / "bench.json")
        manifest["description"] = "Recovered transaction"
        journal = bench / "local" / "transactions" / "tx-recovery.json"
        benchctl.atomic_json(
            journal,
            {
                "schema_version": benchctl.TRANSACTION_VERSION,
                "id": "tx-recovery",
                "created_at": benchctl.now_utc(),
                "writes": [{"path": "bench.json", "value": manifest}],
            },
        )
        with benchctl.bench_write_lock(bench):
            pass
        self.assertEqual(benchctl.load_json(bench / "bench.json")["description"], "Recovered transaction")
        self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()

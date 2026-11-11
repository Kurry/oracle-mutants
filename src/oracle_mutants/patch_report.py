"""Report source-patch mutations graded by a remote Harbor verifier."""

from __future__ import annotations

import json
from collections import Counter
from html import escape
from pathlib import Path

CASE_KINDS = {"reference", "generated", "regression", "alternative"}
TEST_STATUSES = {"passed", "failed", "skipped"}


def read_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _rows_by_id(rows: object, id_key: str) -> dict[str, dict]:
    if not isinstance(rows, list):
        raise ValueError(f"Expected an array of records with {id_key}")
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or not _has_text(row.get(id_key)):
            raise ValueError(f"Missing {id_key} in a report record")
        identifier = row[id_key]
        if identifier in by_id:
            raise ValueError(f"Duplicate {id_key}: {identifier}")
        by_id[identifier] = row
    return by_id


def _load_candidates(manifest: dict) -> dict[str, dict]:
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("Unsupported patch manifest schema version")
    for key in ("base_sha", "task_digest", "verifier_digest"):
        if not _has_text(manifest.get(key)):
            raise ValueError(f"Missing {key} in the patch manifest")
    if not isinstance(manifest.get("generators"), dict):
        raise ValueError("Missing generators object in the patch manifest")

    candidates = _rows_by_id(manifest.get("candidates"), "id")
    for identifier, case in candidates.items():
        if not _has_text(case.get("kind")) or case["kind"] not in CASE_KINDS:
            raise ValueError(f"Invalid case kind for {identifier}")
        if not all(_has_text(case.get(key)) for key in ("label", "patch_sha256")):
            raise ValueError(f"Missing label or patch digest for {identifier}")
        expected = case.get("expected_failed_tests")
        if not isinstance(expected, list) or not all(_has_text(name) for name in expected):
            raise ValueError(f"Invalid expected_failed_tests for {identifier}")
        if expected and case["kind"] != "regression":
            raise ValueError(f"Only saved regressions can specify expected_failed_tests: {identifier}")
    if sum(case["kind"] == "reference" for case in candidates.values()) != 1:
        raise ValueError("The patch manifest must contain exactly one reference control")
    return candidates


def _reward(grade: dict) -> int | float | None:
    reward = grade.get("reward")
    if type(reward) in (int, float) and 0 <= reward <= 1:
        return reward
    return None


def _grade_problem(grade: dict) -> str | None:
    if not grade:
        return "No grade was returned for this candidate"
    if grade.get("error"):
        return f"Grader error: {grade['error']}"
    grade_status = grade.get("grade_status")
    if grade_status is not None and grade_status != "graded":
        reason = grade.get("reject_reason")
        detail = f" ({reason})" if reason else ""
        return f"Verifier status: {grade_status}{detail}"
    if grade.get("status") != "completed" or grade.get("valid") is not True:
        return "The grader did not complete a valid run"
    reward = _reward(grade)
    if reward is None:
        return "The grader returned no reward between 0 and 1"

    tests = grade.get("tests")
    if not isinstance(tests, list) or not tests:
        return "No test results were reported"
    if any(
        not isinstance(test, dict)
        or not _has_text(test.get("name"))
        or not _has_text(test.get("status"))
        or test["status"] not in TEST_STATUSES
        for test in tests
    ):
        return "The test readout contains an invalid or unfinished result"
    if all(test["status"] == "skipped" for test in tests):
        return "All reported tests were skipped"

    has_failure = any(test["status"] == "failed" for test in tests)
    if reward == 1 and has_failure:
        return "Full reward conflicts with the reported test failures"
    if reward != 1 and not has_failure:
        return "The reduced reward has no reported failing test"
    return None


def _unobserved_tests(reference_tests: list[dict], tests: list[dict]) -> list[str]:
    """Require the reference's reported tests and execution to be represented."""
    missing = Counter(test["name"] for test in reference_tests) - Counter(test["name"] for test in tests)
    newly_skipped = Counter(test["name"] for test in reference_tests if test["status"] != "skipped") - Counter(
        test["name"] for test in tests if test["status"] != "skipped"
    )
    return sorted(missing.keys() | newly_skipped.keys())


def _outcome(case: dict, grade: dict, reference_tests: list[dict] | None) -> tuple[str, str]:
    problem = _grade_problem(grade)
    if problem:
        return "inconclusive", problem
    reward = _reward(grade)
    kind = case["kind"]
    if kind == "reference":
        if reward == 1:
            return "reference_passed", "Reference earned full reward"
        return "inconclusive", f"Reference earned {reward} instead of full reward"
    if reference_tests is None:
        return "inconclusive", "The reference control did not pass"

    unobserved = _unobserved_tests(reference_tests, grade["tests"])
    if unobserved:
        return "inconclusive", f"Missing or newly skipped reference tests: {', '.join(unobserved)}"
    if kind == "generated":
        if reward == 1:
            return "survived", "Full reward. Review the observable behavior before treating this as a gap"
        return "rejected", "The verifier reported failing tests and reduced the reward"
    if kind == "alternative":
        if reward == 1:
            return "alternative_accepted", "Saved valid alternative earned full reward"
        return "alternative_rejected", "Saved valid alternative lost reward. Review for verifier overconstraint"
    if reward == 1:
        return "regression_survived", "Saved functional regression earned full reward"

    failed = {test["name"] for test in grade["tests"] if test["status"] == "failed"}
    missing_failures = sorted(set(case["expected_failed_tests"]) - failed)
    if missing_failures:
        return "inconclusive", f"Reduced reward without the expected failures: {', '.join(missing_failures)}"
    return "regression_caught", "Saved functional regression was rejected with the expected test evidence"


def _result(case: dict, grade: dict, reference_tests: list[dict] | None) -> dict:
    status, reason = _outcome(case, grade, reference_tests)
    tests = grade.get("tests")
    tests = tests if isinstance(tests, list) else []
    fields = ("id", "kind", "label", "patch_sha256", "path", "operator", "expected_failed_tests")
    return {
        **{key: case[key] for key in fields if key in case},
        "status": status,
        "reason": reason,
        "reward": _reward(grade),
        "tests_reported": len(tests),
        "failed_tests": sorted({
            test["name"] for test in tests
            if isinstance(test, dict) and _has_text(test.get("name")) and test.get("status") == "failed"
        }),
    }


def _markdown_cell(value: object) -> str:
    return escape(" ".join(str(value).splitlines()), quote=False).replace("\\", "\\\\").replace("|", "\\|")


def _markdown(report: dict) -> str:
    lines = [
        "# Source-patch mutation report",
        "",
        f"Batch status: **{report['status']}**",
        "",
        "| Input | Value |",
        "| --- | --- |",
    ]
    for name in ("base_sha", "task_head", "task_digest", "verifier_digest", "reference_patch_sha256", "generators"):
        if name in report:
            value = json.dumps(report[name], sort_keys=True) if name == "generators" else report[name]
            lines.append(f"| {_markdown_cell(name)} | {_markdown_cell(value)} |")
    lines.extend([
        "",
        "| Case | Outcome | Reward | Tests reported | Evidence |",
        "| --- | --- | --- | --- | --- |",
    ])
    for row in report["results"]:
        reward = row["reward"] if row["reward"] is not None else "—"
        evidence = [row["reason"]]
        if row["expected_failed_tests"]:
            evidence.append("Expected failures: " + ", ".join(row["expected_failed_tests"]))
        if row["failed_tests"]:
            evidence.append("Observed failures: " + ", ".join(row["failed_tests"]))
        cells = (row["label"], row["status"], reward, row["tests_reported"], ". ".join(evidence))
        lines.append("| " + " | ".join(map(_markdown_cell, cells)) + " |")
    return "\n".join(lines) + "\n"


def write_patch_report(manifest_path: Path, grades_path: Path, out_dir: Path) -> int:
    """Join a patch manifest with the matching remote regrade by candidate ID."""
    manifest = read_object(manifest_path)
    candidates = _load_candidates(manifest)
    grades = read_object(grades_path)
    if grades.get("regraded") is not True:
        raise ValueError("Expected a remote regrade report with regraded=true")
    by_id = _rows_by_id(grades.get("attempts"), "trajectory_id")
    unknown = sorted(by_id.keys() - candidates.keys())
    if unknown:
        raise ValueError(f"Grade IDs absent from the patch manifest: {', '.join(unknown)}")

    reference = next(case for case in candidates.values() if case["kind"] == "reference")
    reference_grade = by_id.get(reference["id"], {})
    reference_status, _ = _outcome(reference, reference_grade, None)
    reference_tests = reference_grade["tests"] if reference_status == "reference_passed" else None
    results = [_result(case, by_id.get(case["id"], {}), reference_tests) for case in candidates.values()]
    incomplete = any(row["status"] == "inconclusive" for row in results)
    report = {
        **{key: manifest[key] for key in (
            "schema_version", "base_sha", "task_head", "task_digest", "verifier_digest", "reference_patch_sha256", "generators"
        ) if key in manifest},
        "status": "inconclusive" if incomplete else "complete",
        "counts": dict(sorted(Counter(row["status"] for row in results).items())),
        "results": results,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "patch-report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (out_dir / "patch-report.md").write_text(_markdown(report), encoding="utf-8")
    return 2 if incomplete else 0

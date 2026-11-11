import json
from pathlib import Path

import pytest

from oracle_mutants.patch_report import write_patch_report

TEST_NAMES = ("stores the title", "displays the stored title")


def candidate(identifier: str, kind: str, expected: list[str] | None = None) -> dict:
    return {
        "id": identifier,
        "kind": kind,
        "label": identifier,
        "patch_sha256": "b" * 64,
        "expected_failed_tests": expected or [],
    }


def grade(identifier: str, reward: float = 1, failed: tuple[str, ...] = (), **updates) -> dict:
    return {
        "trajectory_id": identifier,
        "status": "completed",
        "valid": True,
        "reward": reward,
        "error": None,
        "tests": [{"name": name, "status": "failed" if name in failed else "passed"} for name in TEST_NAMES],
        **updates,
    }


def write_inputs(tmp_path: Path, cases: list[dict], rows: list[dict]) -> tuple[Path, Path, Path]:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 1,
        "base_sha": "a" * 40,
        "task_head": "c" * 40,
        "task_digest": "d" * 64,
        "verifier_digest": "e" * 64,
        "generators": {"oracle-mutants": "0.1.0"},
        "candidates": cases,
    }), encoding="utf-8")
    grades_path = tmp_path / "regraded.json"
    grades_path.write_text(json.dumps({"regraded": True, "attempts": rows}), encoding="utf-8")
    return manifest_path, grades_path, tmp_path / "out"


def run_report(tmp_path: Path, cases: list[dict], rows: list[dict]) -> tuple[int, dict, str]:
    manifest, grades, out = write_inputs(tmp_path, cases, rows)
    exit_code = write_patch_report(manifest, grades, out)
    report = json.loads((out / "patch-report.json").read_text(encoding="utf-8"))
    markdown = (out / "patch-report.md").read_text(encoding="utf-8")
    return exit_code, report, markdown


def test_report_joins_by_id_and_separates_completed_outcomes(tmp_path):
    expected_failure = TEST_NAMES[1]
    cases = [
        candidate("reference", "reference"),
        candidate("new survivor", "generated"),
        candidate("new rejection", "generated"),
        candidate("known failure", "regression", [expected_failure]),
        candidate("known survivor", "regression", [expected_failure]),
        candidate("accepted alternative", "alternative"),
        candidate("rejected alternative", "alternative"),
    ]
    rows = [
        grade("new survivor"),
        grade("known failure", 0, (expected_failure,)),
        grade("known survivor"),
        grade("accepted alternative"),
        grade("rejected alternative", 0, (TEST_NAMES[0],)),
        grade("reference"),
        grade("new rejection", 0, (TEST_NAMES[0],)),
    ]
    code, report, markdown = run_report(tmp_path, cases, rows)

    assert code == 0
    assert report["status"] == "complete"
    assert [row["status"] for row in report["results"]] == [
        "reference_passed", "survived", "rejected", "regression_caught", "regression_survived",
        "alternative_accepted", "alternative_rejected",
    ]
    assert report["results"][3]["failed_tests"] == [expected_failure]
    assert report["task_head"] == "c" * 40
    assert report["verifier_digest"] == "e" * 64
    assert "Observed failures: displays the stored title" in markdown
    assert "alternative_rejected" in markdown


def test_report_requires_a_passing_reference(tmp_path):
    code, report, _ = run_report(
        tmp_path,
        [candidate("reference", "reference"), candidate("survivor", "generated")],
        [grade("reference", 0, (TEST_NAMES[0],)), grade("survivor")],
    )

    assert code == 2
    assert report["status"] == "inconclusive"
    assert [row["status"] for row in report["results"]] == ["inconclusive", "inconclusive"]
    assert report["results"][1]["reason"] == "The reference control did not pass"


@pytest.mark.parametrize(("rows", "reason"), [
    ([], "No grade"),
    ([grade("probe", error="image build failed")], "Grader error"),
    ([grade("probe", status="running")], "did not complete"),
    ([grade("probe", valid=False)], "did not complete"),
    ([grade("probe", 0, grade_status="patch_rejected", reject_reason="unapplied")], "patch_rejected (unapplied)"),
    ([grade("probe", 0, grade_status="unfinished")], "unfinished"),
    ([grade("probe", float("nan"))], "no reward"),
    ([grade("probe", tests=[])], "No test results"),
    ([grade("probe", tests=[{"name": TEST_NAMES[0], "status": "pending"}])], "unfinished result"),
    ([grade("probe", tests=[{"name": TEST_NAMES[0], "status": "passed"}])], "Missing or newly skipped"),
    ([grade("probe", tests=[{"name": TEST_NAMES[0], "status": "passed"},
                           {"name": TEST_NAMES[1], "status": "skipped"}])], "Missing or newly skipped"),
    ([grade("probe", 0)], "no reported failing test"),
    ([grade("probe", 1, (TEST_NAMES[0],))], "conflicts with the reported test failures"),
])
def test_incomplete_grades_are_not_counted_as_passes_or_rejections(tmp_path, rows, reason):
    code, report, _ = run_report(
        tmp_path,
        [candidate("reference", "reference"), candidate("probe", "generated")],
        [grade("reference"), *rows],
    )

    assert code == 2
    assert report["status"] == "inconclusive"
    assert report["results"][1]["status"] == "inconclusive"
    assert reason in report["results"][1]["reason"]


def test_regression_requires_the_named_failure(tmp_path):
    code, report, _ = run_report(
        tmp_path,
        [candidate("reference", "reference"), candidate("probe", "regression", [TEST_NAMES[1]])],
        [grade("reference"), grade("probe", 0, (TEST_NAMES[0],))],
    )

    assert code == 2
    assert report["results"][1]["status"] == "inconclusive"
    assert report["results"][1]["reason"] == f"Reduced reward without the expected failures: {TEST_NAMES[1]}"


def test_skipped_reference_tests_can_stay_skipped(tmp_path):
    tests = [{"name": TEST_NAMES[0], "status": "passed"}, {"name": TEST_NAMES[1], "status": "skipped"}]
    code, report, _ = run_report(
        tmp_path,
        [candidate("reference", "reference"), candidate("probe", "generated")],
        [grade("reference", tests=tests), grade("probe", tests=tests)],
    )

    assert code == 0
    assert report["results"][1]["status"] == "survived"


@pytest.mark.parametrize(("cases", "rows", "message"), [
    ([candidate("reference", "reference"), candidate("reference", "generated")], [], "Duplicate id"),
    ([candidate("reference", "reference")], [grade("reference"), grade("reference")], "Duplicate trajectory_id"),
    ([candidate("reference", "reference")], [grade("unknown")], "IDs absent from the patch manifest"),
    ([candidate("probe", "generated")], [], "exactly one reference"),
])
def test_report_rejects_invalid_id_sets(tmp_path, cases, rows, message):
    manifest, grades, out = write_inputs(tmp_path, cases, rows)
    with pytest.raises(ValueError, match=message):
        write_patch_report(manifest, grades, out)
    assert not out.exists()

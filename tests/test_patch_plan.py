import hashlib
import json
import subprocess

from oracle_mutants.cli import main

BASE_TS = """export const untouched = true
export class Editor {}
"""
REFERENCE_TS = """export const untouched = true
export class Editor {
  title = { text: '' }
  draft = ''
  async attach(api: any, notify: any) {
    try {
      await api.attach()
    } catch (error) {
      notify.error('Échec')
    }
  }
  async save(api: any) {
    const chosenTitle = this.draft.length === 0 ? 'Untitled' : this.draft
    await api.save({ title: chosenTitle })
    this.title.text = chosenTitle
  }
}
"""


def git(repo, *args, data=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=data, stderr=subprocess.PIPE)


def task_with_bundle(tmp_path, files):
    task = tmp_path / "task"
    source = tmp_path / "source"
    source.mkdir()
    (task / "environment").mkdir(parents=True)
    (task / "solution").mkdir()
    (task / "tests").mkdir()
    (task / "tests" / "test.sh").write_text("# synthetic verifier\n")
    git(source, "init", "-q")
    for path, (initial, _) in files.items():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(initial)
    git(source, "add", ".")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Base")
    base = git(source, "rev-parse", "HEAD").decode().strip()
    (task / "task.toml").write_text(f'[metadata]\nbase_commit = "{base}"\n')
    git(source, "bundle", "create", str(task / "environment" / "repo.bundle"), "HEAD")
    for path, (_, replacement) in files.items():
        (source / path).write_text(replacement)
    (task / "solution" / "solution.patch").write_bytes(git(source, "diff", "--binary"))
    git(source, "reset", "--hard", "HEAD")
    return task, source, base


def read_plan(out):
    return [json.loads((out / name).read_text()) for name in ("manifest.json", "changes.json")]


def test_stock_and_opt_in_mutations_are_base_relative(tmp_path):
    relative = "src/editor.ts"
    task, source, base = task_with_bundle(tmp_path, {
        relative: (BASE_TS, REFERENCE_TS),
        "src/editor.spec.ts": ("export const enabled = true\n", "export const enabled = false\n"),
        "vendor/pinned.ts": ("export const enabled = false\n", "export const enabled = true\n"),
    })
    config = tmp_path / "cases.json"
    config.write_text(json.dumps({"recipes": [{"path": relative, "assignment": "this.title.text"}]}))
    outputs = [tmp_path / "batch-a", tmp_path / "batch-b"]
    for out in outputs:
        assert main(["plan-patches", str(task), "--out", str(out), "--cases", str(config)]) == 0
    manifest, changes = read_plan(outputs[0])
    assert (manifest, changes) == tuple(read_plan(outputs[1]))
    assert manifest["base_sha"] == base
    assert manifest["generators"]["stryker"] == "8.7.1"
    assert 2 < len(manifest["candidates"]) <= 13
    assert len({case["patch_sha256"] for case in manifest["candidates"]}) == len(manifest["candidates"])
    saw_recipe = saw_silent_error = False
    for case, attempt in zip(manifest["candidates"], changes["attempts"], strict=True):
        assert case["id"] == attempt["trajectory_id"]
        patch = attempt["patch"].encode()
        assert hashlib.sha256(patch).hexdigest() == case["patch_sha256"]
        git(source, "reset", "--hard", "HEAD")
        git(source, "apply", "--check", data=patch)
        git(source, "apply", data=patch)
        content = (source / relative).read_text()
        assert "export const untouched = true" in content
        assert (source / "src/editor.spec.ts").read_text() == "export const enabled = false\n"
        assert (source / "vendor/pinned.ts").read_text() == "export const enabled = true\n"
        if case["kind"] == "generated":
            assert case["path"] == relative
        if case.get("operator") == "UseRawInputAtDisplayAssignment":
            assert content == REFERENCE_TS.replace("this.title.text = chosenTitle", "this.title.text = this.draft")
            saw_recipe = True
        if case.get("operator") == "BlockStatement" and "catch (error) {}" in content:
            assert "await api.attach()" in content
            assert "notify.error" not in content
            saw_silent_error = True
    assert saw_recipe and saw_silent_error
    out = tmp_path / "bounded"
    assert main(["plan-patches", str(task), "--out", str(out), "--cases", str(config), "--max-mutants", "1"]) == 0
    bounded, _ = read_plan(out)
    assert bounded["candidates"] == manifest["candidates"][:2]


def test_saved_cases_and_python_mutations_deduplicate(tmp_path):
    filename = "logic.py"
    task, source, _ = task_with_bundle(tmp_path, {
        filename: ("def apply(total):\n    return total\n", "def apply(total):\n    return total + 2\n"),
    })
    (source / filename).write_text("def apply(total):\n    return total - 2\n")
    (tmp_path / "regression.patch").write_bytes(git(source, "diff"))
    (source / filename).write_text("def apply(total):\n    extra = 2\n    return extra + total\n")
    (tmp_path / "alternative.patch").write_bytes(git(source, "diff"))
    config = tmp_path / "cases.json"
    case = {"kind": "regression", "label": "Subtracts", "patch": "regression.patch", "expected_failed_tests": ["adds two"]}
    config.write_text(json.dumps({"cases": [
        case, case, {"kind": "alternative", "label": "Uses extra", "patch": "alternative.patch"},
    ]}))
    out = tmp_path / "batch"
    assert main(["plan-patches", str(task), "--out", str(out), "--cases", str(config)]) == 0
    manifest, changes = read_plan(out)
    assert [c["kind"] for c in manifest["candidates"]] == ["reference", "regression", "alternative", "generated"]
    assert manifest["candidates"][1]["expected_failed_tests"] == ["adds two"]
    assert "return total + 3" in changes["attempts"][-1]["patch"]


def test_lfs_pointer_and_bad_patch_are_input_errors(tmp_path, capsys):
    task, _, _ = task_with_bundle(tmp_path, {"logic.py": ("x = 1\n", "x = 2\n")})
    out = tmp_path / "batch"
    (task / "solution/solution.patch").write_text("not a patch\n")
    assert main(["plan-patches", str(task), "--out", str(out)]) == 2
    assert "git apply failed" in capsys.readouterr().err
    (task / "environment/repo.bundle").write_text("version https://git-lfs.github.com/spec/v1\n")
    assert main(["plan-patches", str(task), "--out", str(out)]) == 2
    assert "Git LFS pointer" in capsys.readouterr().err
    assert not out.exists()


def test_verifier_changes_change_candidate_identity(tmp_path):
    task, _, _ = task_with_bundle(tmp_path, {"logic.py": ("x = 1\n", "x = 2\n")})
    out = tmp_path / "batch"
    args = ["plan-patches", str(task), "--out", str(out), "--max-mutants", "0"]
    assert main(args) == 0
    before, _ = read_plan(out)
    (task / "tests/test.sh").write_text("# changed synthetic verifier\n")
    assert main(args) == 0
    after, _ = read_plan(out)
    assert before["verifier_digest"] != after["verifier_digest"]
    assert before["candidates"][0]["id"] != after["candidates"][0]["id"]
    assert before["candidates"][0]["patch_sha256"] == after["candidates"][0]["patch_sha256"]


def test_plan_ignores_git_hook_context(tmp_path, monkeypatch):
    task, source, _ = task_with_bundle(tmp_path, {"logic.py": ("x = 1\n", "x = 2\n")})
    monkeypatch.setenv("GIT_DIR", str(source / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(source))
    out = tmp_path / "batch"
    assert main(["plan-patches", str(task), "--out", str(out)]) == 0
    assert (source / "logic.py").read_text() == "x = 1\n"


def test_plan_rejects_output_inside_task_inputs(tmp_path, capsys):
    task, _, _ = task_with_bundle(tmp_path, {"logic.py": ("x = 1\n", "x = 2\n")})
    out = task / "tests" / "reports"
    assert main(["plan-patches", str(task), "--out", str(out)]) == 2
    assert "outside the task" in capsys.readouterr().err
    assert not out.exists()

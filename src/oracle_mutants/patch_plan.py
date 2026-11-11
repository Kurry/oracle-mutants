"""Generate full, base-relative patches for a remote verifier."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path

from . import __version__, pyast

JS_EXTENSIONS = {".js", ".cjs", ".mjs", ".jsx", ".ts", ".tsx", ".cts", ".mts"}
EXCLUDED_DIRS = {
    "test", "tests", "__tests__", "spec", "specs", "fixture", "fixtures",
    "verifier", "vendor", "node_modules", "deps", "third_party", "third-party", "generated", "build", "dist", "coverage",
}
HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
JS_TOOL = Path(__file__).with_name("js")
DEFAULT_MAX_MUTANTS = 12
DIFF = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color")


def _git(cwd: Path, *args: str, patch: bytes | None = None) -> bytes:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    proc = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.autocrlf=false", "-C", str(cwd), *args],
        input=patch, capture_output=True, env=env,
    )
    if proc.returncode:
        raise ValueError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _required(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"task input is a symlink: {path}")
    if not path.is_file():
        raise ValueError(f"missing {path}")
    with path.open("rb") as file:
        if file.read(len(LFS_POINTER)) == LFS_POINTER:
            raise ValueError(f"Git LFS pointer at {path}. Fetch the task's Git LFS objects first.")
    return path


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    with _required(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _input_digests(task: Path) -> tuple[str, str]:
    inputs = {}
    for directory in ("environment", "solution", "tests"):
        root = task / directory
        if root.is_symlink():
            raise ValueError(f"task input is a symlink: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"task input is a symlink: {path}")
            if path.is_file():
                inputs[path.relative_to(task).as_posix()] = _file_digest(path)
    for name in ("task.toml", "instruction.md"):
        path = task / name
        if path.is_file():
            inputs[name] = _file_digest(path)
    verifier = {name: digest for name, digest in inputs.items() if name.startswith("tests/")}
    return _digest(json.dumps(inputs, sort_keys=True).encode()), _digest(json.dumps(verifier, sort_keys=True).encode())


def _config(path: Path | None) -> tuple[list[dict], list[dict]]:
    if path is None:
        return [], []
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or set(config) - {"cases", "recipes"}:
        raise ValueError("cases file must be an object with optional cases and recipes arrays")
    cases = config.get("cases", [])
    recipes = config.get("recipes", [])
    if not isinstance(cases, list) or not isinstance(recipes, list):
        raise ValueError("cases and recipes must be arrays")
    for recipe in recipes:
        if (not isinstance(recipe, dict) or set(recipe) != {"path", "assignment"}
                or any(not isinstance(v, str) or not v.strip() for v in recipe.values())):
            raise ValueError("each recipe needs a source path and an assignment target")
    root = path.resolve().parent
    loaded = []
    for row in cases:
        if not isinstance(row, dict):
            raise ValueError("each saved case must be an object")
        kind, label, relative = row.get("kind"), row.get("label"), row.get("patch")
        tests = row.get("expected_failed_tests", [])
        if kind not in ("regression", "alternative") or not isinstance(label, str) or not label.strip():
            raise ValueError("each saved case needs a label and kind regression or alternative")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError(f"{label}: patch must be a path relative to the cases file")
        patch = (root / relative).resolve()
        if not patch.is_relative_to(root):
            raise ValueError(f"{label}: patch must be inside the cases directory")
        if (not isinstance(tests, list) or any(not isinstance(t, str) or not t.strip() for t in tests)
                or (tests and kind != "regression")):
            raise ValueError(f"{label}: expected_failed_tests must be an array of test names for a regression")
        loaded.append({
            "kind": kind, "label": label, "expected_failed_tests": sorted(set(tests)),
            "patch": _required(patch).read_bytes(),
        })
    return loaded, recipes


def _source_files(repo: Path) -> list[dict]:
    changed = _git(repo, *DIFF, "--cached", "--name-only", "-z", "HEAD").split(b"\0")
    sources = []
    for relative in sorted(os.fsdecode(p) for p in changed if p):
        path = Path(relative)
        file = repo / path
        if (path.suffix.lower() not in JS_EXTENSIONS | {".py"} or file.is_symlink() or not file.is_file()
                or any(part.lower() in EXCLUDED_DIRS or part.startswith(".") for part in path.parts[:-1])
                or path.name.lower().startswith(("test_", "conftest.")) or path.name.lower().endswith(".d.ts")
                or any(s in path.stem.lower().split(".") for s in ("test", "spec", "generated", "min"))):
            continue
        source = file.read_bytes()
        if b"\x00" in source or source.startswith(LFS_POINTER):
            continue
        try:
            content = source.decode("utf-8")
        except UnicodeDecodeError:
            continue
        ranges = []
        for line in _git(repo, *DIFF, "--cached", "--unified=0", "HEAD", "--", relative).splitlines():
            if match := HUNK.match(line):
                start = int(match[1])
                count = int(match[2]) if match[2] is not None else 1
                if count:
                    ranges.append([start, start + count - 1])
        if ranges:
            sources.append({"path": relative, "source": content, "ranges": ranges})
    return sources


def _source_mutations(sources: list[dict], recipes: list[dict]) -> tuple[list[dict], dict]:
    js = [f for f in sources if Path(f["path"]).suffix.lower() in JS_EXTENSIONS]
    js_paths = {f["path"] for f in js}
    if any(recipe["path"] not in js_paths for recipe in recipes):
        raise ValueError("each recipe must target a changed JavaScript or TypeScript production file")
    versions = {"oracle-mutants": __version__}
    mutations = []
    if js:
        try:
            proc = subprocess.run(
                ["node", str(JS_TOOL / "generate.mjs")],
                input=json.dumps({"files": js, "recipes": recipes}), capture_output=True, text=True,
            )
        except FileNotFoundError as exc:
            raise ValueError("Node.js is required for JavaScript and TypeScript mutations") from exc
        if proc.returncode:
            raise ValueError(f"Stryker generation failed. Install dependencies with npm ci in {JS_TOOL}.\n{proc.stderr.strip()}")
        result = json.loads(proc.stdout)
        versions["stryker"] = result["version"]
        mutations.extend(result["mutations"])
    for file in sources:
        if Path(file["path"]).suffix.lower() != ".py":
            continue
        source = file["source"]
        for site in pyast.sites(source):
            start_line = source.count("\n", 0, site.start) + 1
            end_line = source.count("\n", 0, max(site.start, site.end - 1)) + 1
            if any(lo <= start_line <= end_line <= hi for lo, hi in file["ranges"]):
                mutations.append({
                    "path": file["path"], "start": len(source[:site.start].encode()),
                    "end": len(source[:site.end].encode()), "replacement": site.replacement, "operator": site.operator,
                })
    mutations.sort(key=lambda m: (
        m["operator"] != "UseRawInputAtDisplayAssignment", m["path"], m["start"], m["end"], m["operator"], m["replacement"],
    ))
    return mutations, versions


def _export_patch(repo: Path) -> bytes:
    return _git(repo, *DIFF, "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/", "HEAD")


def plan_patches(
    task: Path, out: Path, cases_path: Path | None = None, max_mutants: int = DEFAULT_MAX_MUTANTS,
) -> int:
    """Write remote regrade inputs and their manifest without executing task code."""
    if max_mutants < 0:
        raise ValueError("--max-mutants must be zero or greater")
    config = tomllib.loads(_required(task / "task.toml").read_text())
    metadata = config.get("metadata")
    base = metadata.get("base_commit") if isinstance(metadata, dict) else None
    if not isinstance(base, str) or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base) is None:
        raise ValueError("task.toml needs a full [metadata].base_commit SHA")
    base = base.lower()
    bundle = _required(task / "environment" / "repo.bundle").resolve()
    reference = _required(task / "solution" / "solution.patch").read_bytes()
    cases, recipes = _config(cases_path)
    task_digest, verifier_digest = _input_digests(task)
    manifest = {
        "schema_version": 1, "base_sha": base, "task_digest": task_digest,
        "verifier_digest": verifier_digest, "candidates": [],
    }
    attempts = []
    seen: dict[str, dict] = {}

    def add(kind: str, label: str, patch: bytes, tests: list[str], **detail: str) -> bool:
        if not patch:
            if kind == "generated":
                return False
            raise ValueError(f"{label}: patch makes no changes to the base")
        digest = _digest(patch)
        if previous := seen.get(digest):
            if kind != "generated" and (kind, tests) != (previous["kind"], previous["expected_failed_tests"]):
                raise ValueError(f"{label}: patch duplicates {previous['label']} with different expectations")
            return False
        candidate_id = "patch-" + _digest(f"{task_digest}:{digest}".encode())
        row = {
            "id": candidate_id, "kind": kind, "label": label, "patch_sha256": digest,
            "expected_failed_tests": tests, **detail,
        }
        manifest["candidates"].append(row)
        seen[digest] = row
        attempts.append({"trajectory_id": candidate_id, "solver": kind, "patch": patch.decode("utf-8")})
        return True

    with tempfile.TemporaryDirectory(prefix="oracle-mutants-") as tmp:
        repo = Path(tmp) / "source"
        _git(Path(tmp), "clone", "--quiet", "--no-checkout", str(bundle), str(repo))
        _git(repo, "checkout", "--quiet", "--detach", base)
        _git(repo, "apply", "--index", "-", patch=reference)
        add("reference", "Reference", _export_patch(repo), [])
        for case in cases:
            _git(repo, "reset", "--hard", base)
            try:
                _git(repo, "apply", "--index", "-", patch=case["patch"])
            except ValueError as exc:
                raise ValueError(f"{case['label']}: {exc}") from exc
            add(case["kind"], case["label"], _export_patch(repo), case["expected_failed_tests"])
        if cases:
            _git(repo, "reset", "--hard", base)
            _git(repo, "apply", "--index", "-", patch=reference)
        sources = _source_files(repo) if max_mutants else []
        mutations, versions = _source_mutations(sources, recipes if max_mutants else [])
        manifest["generators"] = versions
        generated = 0
        for mutation in mutations:
            if generated >= max_mutants:
                break
            file = repo / mutation["path"]
            data = file.read_bytes()
            start, end = mutation["start"], mutation["end"]
            file.write_bytes(data[:start] + mutation["replacement"].encode() + data[end:])
            try:
                if add("generated", f"{mutation['operator']} in {mutation['path']}", _export_patch(repo), [],
                       path=mutation["path"], operator=mutation["operator"]):
                    generated += 1
            finally:
                file.write_bytes(data)
    out.mkdir(parents=True, exist_ok=True)
    (out / "changes.json").write_text(json.dumps({"attempts": attempts}, indent=2) + "\n")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return len(attempts)

# oracle-mutants

**Find benchmark verifiers that accept wrong answers.**

An agent benchmark is only as good as its verifiers. If a verifier passes a wrong answer, a model can score points it did not earn, leaderboards drift, and RL training rewards the bug. Verifier bugs like these keep turning up in public benchmarks: a program that just prints a fixed string passing a task, or a matrix of zeros accepted as a solution.

`oracle-mutants` tests a task's verifier the way mutation testing tests a test suite:

1. Run the task's **reference solution** once with [Harbor](https://www.harborframework.com) and collect the files it produces.
2. Make **plausible-but-wrong variants** of those files: half the records dropped, every number zeroed, one value off by one, a JSON key removed, a comparison flipped in submitted code, a corrupted binary.
3. Re-run the **real verifier** on each variant in a fresh container, exactly as Harbor's separate verifier mode does.
4. Report every variant the verifier still gives full reward to. These are the **surviving mutants**.

It needs no API keys and no LLM, and it is deterministic: the same task always gets the same mutants.

## Example

`examples/word-count-weak` is a small task whose verifier only checks the *shape* of the answer:

```
$ oracle-mutants run examples/word-count-weak
[1/4] running the reference solution of oracle-mutants/word-count-weak with Harbor ...
      oracle reward 1.0; exported: /app/counts.json
[2/4] building the verifier image from tests/ ...
[3/4] re-verifying the untouched reference outputs ...
[4/4] running 10 mutants with 4 workers ...
      rejected  strong empty              /app/counts.json
      rejected  strong empty-container    /app/counts.json
      ACCEPTED  strong numbers-to-zero    /app/counts.json
      ACCEPTED  medium drop-key           /app/counts.json
      ...
8 of 10 mutants accepted (3s). Report: results/examples/word-count-weak.md
```

The report shows each accepted mutant as a diff. In this case, setting every count to zero still earns full reward:

```diff
 {
-  "a": 2,
-  "and": 1,
+  "a": 0,
+  "and": 0,
   ...
-  "the": 4
+  "the": 0
 }
```

A strong verifier rejects all of them. On Terminal-Bench's `foodstuff-beta-activity`, all 8 mutants are rejected in about 3 seconds.

## Results on Terminal-Bench (October 2026)

Every number here comes from a run report committed in [`results/`](results/) (machine paths removed by
`scripts/export_results.py`), and `tests/test_results.py` recomputes each one from those files.

- **A new verifier gap, reported upstream.** In `session-window-debug`, a merger that gives a merged
  session the wrong end time scores 1.0 in Harbor (7/7 tests): two `min`/`max` swaps in `merger.py`
  are accepted ([report](results/final/session-window-debug.json)). Reported with a reproduction and a
  fix: [terminal-bench#2243](https://github.com/harbor-framework/terminal-bench/issues/2243).
- **Precision.** First pass: 15 tasks, 165 mutants, 36 accepted ([reports](results/first-pass/); one
  task's run stopped before writing its report, and its log holds every verdict). Every accepted mutant
  was triaged by reading the verifier; none was a gap. Each false-alarm category became a rule in the
  tool ([triage notes](docs/triage-notes.md)).
- **On cargo-flight-dispatch**, the final run: 169 mutants, 59 accepted, 13 change the output: 10 change
  the flight plan's legs and 3 only a summary total, by 0.2% or less
  ([report](results/final/cargo-flight-dispatch.json)).
- **Recall against a published manual audit**
  ([terminal-bench#2150](https://github.com/harbor-framework/terminal-bench/issues/2150)), with
  predictions committed before the run ([pre-registration](docs/recall-preregistration.md)). The
  blind run found none of the seven findings in reach ([reports](results/recall-blind/)). The improved
  version, whose operators were chosen after reading the findings' titles, re-finds five published
  findings: four of the seven predicted in reach, plus one predicted out of reach
  ([results](docs/recall-results.md)). Full
  write-up: [Do benchmark verifiers reject wrong answers?](https://fikrialfaraby.com/notes/verifier-mutants/)

## Install and run

Needs Python 3.11+, Docker, and the Harbor CLI (`uv tool install harbor`).

```bash
uv tool install git+https://github.com/FikriAlfaraby/oracle-mutants
oracle-mutants run path/to/task            # runs the oracle with Harbor, then the mutants
oracle-mutants run path/to/task --dry-run  # list the mutants only
oracle-mutants run path/to/task --oracle-job jobs/<job>/<trial>   # reuse an earlier oracle run
```

Useful flags: `--severity strong` (run only near-certain mutants), `--max-mutants 40`, `--max-files 6` (files sampled inside directory artifacts), `--workers 4`, `--probe` (measure how far an accepted number can move, see below).

For tasks whose answer is code:

```bash
# every site, not three per operator; also mutate code the reference left unchanged
oracle-mutants run path/to/task --exhaustive --include-unchanged-code --max-mutants 100000 \
  --compare "python /app/dispatch.py --output /output/plan.json" --compare-file /output/plan.json
```

`--compare` reruns the program with each accepted mutant. A mutant that writes exactly the reference
output (or the same JSON data) is equivalent and is set aside; one whose output changed and still got
full reward is reported first, largest change first. `oracle-mutants compare` does the same after the
fact for an earlier run.

The exit code is `1` when a strong mutant survives, so it can gate CI for new task pull requests.

## Source-patch tasks with a remote verifier

Use this workflow for Harbor tasks that submit a code patch. The task supplies
`[metadata].base_commit` in `task.toml`, a materialized `environment/repo.bundle`, and
`solution/solution.patch`. Use the same task directory and effective tests you intend to grade.

From a checkout of this fork, install the Python CLI. For JavaScript or TypeScript generation,
also install the pinned Stryker tooling with Node.js 18 or later.

```bash
uv sync
npm ci --prefix src/oracle_mutants/js --ignore-scripts
uv run oracle-mutants plan-patches /path/to/task --out /path/to/private-batch
```

Planning applies the reference in a temporary checkout and generates variants from its changed
production lines. It uses the Python AST operators for Python and StrykerJS for JS/TS. By default it
exports up to 12 generated candidates, plus the reference and any saved cases. Use `--max-mutants 0`
for a batch containing only the reference and saved cases. Planning and reporting run without
building the task image or executing task code.

Pass `--cases /path/to/cases.json` to add saved full patches against the original base or an optional
mutation recipe. Patch paths are relative to the cases file.

```json
{
  "cases": [
    {
      "kind": "regression",
      "label": "Failed save leaves the displayed value stale",
      "patch": "stale-display.patch",
      "expected_failed_tests": ["shows the persisted value after recovery"]
    },
    {
      "kind": "alternative",
      "label": "Uses a reducer to apply the saved response",
      "patch": "reducer.patch"
    }
  ],
  "recipes": [
    { "path": "src/editor.ts", "assignment": "this.title.text" }
  ]
}
```

The recipe finds a changed assignment that uses a local value resolved by a conditional fallback.
It uses the raw input from the conditional's alternate branch at that assignment. Other uses of the
resolved value, such as the save request, are preserved. Ambiguous or unsupported assignments
produce an input error. `expected_failed_tests` is optional for saved regressions and uses the
verifier's exact reported names.

Use the generated `changes.json` with your authorized remote grader. In a SWE-Enterprise template
checkout with access to the deployed runner, the replay command is:

```bash
scripts/modal/ci_run regrade \
  --task-dir /path/to/task \
  --attempts /path/to/private-batch/changes.json \
  --out /path/to/private-batch/regraded.json
```

Use the same effective task for planning and grading, including any canonical verifier overlay.
Run the reporting command with the matching batch manifest:

```bash
uv run oracle-mutants report-patches \
  --manifest /path/to/private-batch/manifest.json \
  --grades /path/to/private-batch/regraded.json \
  --out /path/to/private-batch
```

The JSON and Markdown reports join grades by candidate ID and require a complete, passing reference.
A generated candidate that earns full reward is a lead for review. Saved regressions and valid
alternatives have separate outcomes. The patch report returns 0 for complete evidence and 2 for
invalid input or an inconclusive batch. Keep private bundles, patches, and reports in private storage.

## Mutation operators

| Output type | Operators |
|---|---|
| Text / CSV / TSV | empty, keep first half of records, drop last record, duplicate a record, swap two records, zero every number, nudge one number (+1 or ×1.5), CRLF + BOM |
| JSON | empty object/array, drop a top-level key, keep half of a list, duplicate a list element, zero every number, nudge one number, set a value to null |
| Python | from the syntax tree: relational replacement (boundary and negation), condition → `True`/`False`, `+`↔`-` and `*`↔`/`, constant +1, flip a boolean, `min`↔`max` and `any`↔`all`. Never inside comments, strings or docstrings; rounding precision and JSON indent are left alone |
| Other code (`.ts`, `.c`, `.rs`, …) | empty file, flip a comparison (`==`↔`!=`, `<`↔`<=`), off-by-one integer constant, flip a boolean, never inside comments or strings |
| Binary | keep the first half of the bytes, invert 16 bytes in the middle |

Each mutant has a **severity**:

- **strong**: almost never a correct answer, so a survivor is very likely a verifier gap.
- **medium**: usually wrong, but some tasks rightly tolerate it.
- **weak**: formatting or order changes that a verifier may legitimately ignore.

A surviving mutant is a lead for a human to check, not a verdict.

## Keeping false alarms down

Most of the work in a tool like this is not finding survivors, it is not wasting a reviewer's time on
survivors that mean nothing. Running it across real Terminal-Bench tasks taught it three rules:

- **Inputs are not answers.** Some tasks export input files as artifacts (so the verifier can read
  them). A file byte-identical to one shipped in `environment/` is skipped and listed in the report.
  The file budget for directory artifacts goes only to files the solution actually wrote.
- **Prose is not code.** Code operators blank every comment, string and docstring before picking a
  site, so `(mod 26)` in a docstring is never turned into `(mod 27)`.
- **Dates and IDs are not values.** The number nudge prefers a standalone value (`, 24);`,
  `<octave>4</octave>`) over a digit inside a date, a timestamp or an ID, and keeps zero padding.

### Tolerance probe

A nudged number that survives is the most common lead, and the slowest to judge: you have to open the
test to find its tolerance. `--probe` measures it instead. It keeps widening the change until the
verifier rejects it:

```
probe /results/TB3_Conf_Answers.csv: accepted +1, rejected +2: the verifier has a tolerance between those two
probe /app/counts.json: accepted every step up to +1000: this value looks unchecked
```

The first line is a documented ±1 tolerance and can be closed in seconds. The second is a value
nobody checks.

## How it stays faithful to Harbor

- It only targets tasks in Harbor's **separate verifier mode** (Terminal-Bench 4.0 format), where the verifier sees nothing but the declared `artifacts`. That makes the artifacts the complete interface to mutate.
- Before running any mutant, it re-verifies the untouched reference outputs and stops if the reward differs from Harbor's own oracle run.
- The verifier image is built from the task's `tests/Dockerfile`. Artifacts are copied in at their original absolute paths, and `/tests/test.sh` writes the reward to `/logs/verifier`, as in Harbor.

## Limitations

- Shared-mode tasks (verifier runs in the agent container) are not supported yet.
- Tasks that need GPUs, sidecar services or cloud sandboxes are out of scope for a laptop run.
- Mutants can be *equivalent*: the change does not alter what the verifier is entitled to judge (a tie-break `<` vs `<=`, a duplicated bottom layer that renders the same pixels, a rule that never fires on any input). A survivor needs a human look before anything is reported.
- Directory artifacts are sampled (`--max-files`). Big code trees are not exhaustively mutated.

## Related work

- Abrich et al., *Finding Blind Spots in AppWorld and WorkArena Task Verifiers* (NeurIPS 2026): source-informed mutation tests for agent benchmark checkers.
- *Before the Model Learns the Bug: Fuzzing RLVR Verifiers* (arXiv 2606.01066).
- Terminal-Bench's own review tooling (`/cheat`, `/fortify`) uses LLM agents to attack tasks. oracle-mutants is a cheap, deterministic complement that runs before them.

## License

Apache-2.0

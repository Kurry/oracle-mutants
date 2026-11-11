"""Command line: oracle-mutants run <task-dir>."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .compare import accepted_mutants, compare
from .compare import to_markdown as compare_markdown
from .operators import MEDIUM, STRONG, WEAK
from .oracle import OracleError, load_oracle_job, run_oracle
from .patch_plan import DEFAULT_MAX_MUTANTS, plan_patches
from .patch_report import write_patch_report
from .probe import probe
from .report import summarize, write
from .runner import Report, execute, plan
from .task import TaskError, load_task
from .verifier import VerifierError, build_image, run_verifier


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="oracle-mutants", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="mutate one task's reference outputs and re-run its verifier")
    r.add_argument("task", type=Path, help="task directory containing task.toml")
    r.add_argument("--oracle-job", type=Path, help="reuse an existing Harbor oracle job/trial directory")
    r.add_argument("--out", type=Path, default=Path("runs"), help="output directory (default: ./runs)")
    r.add_argument("--max-mutants", type=int, default=40)
    r.add_argument("--max-files", type=int, default=6, help="files sampled inside each directory artifact")
    r.add_argument("--severity", default="strong,medium,weak", help="comma list of severities to run")
    r.add_argument("--workers", type=int, default=4, help="verifier containers to run in parallel")
    r.add_argument("--dry-run", action="store_true", help="list the mutants without running anything in Docker")
    r.add_argument("--probe", action="store_true",
                   help="for each accepted nudged number, widen the change until the verifier rejects it")
    r.add_argument("--exhaustive", action="store_true",
                   help="mutate every code site instead of three per operator (raise --max-mutants to match)")
    r.add_argument("--include-unchanged-code", action="store_true",
                   help="also mutate code files the reference solution left unchanged (the agent may edit them too)")
    r.add_argument("--compare", metavar="CMD",
                   help="command that runs the submitted program; each accepted mutant is re-run with it to see "
                        "whether its output differs from the reference (needs --compare-file)")
    r.add_argument("--compare-file", metavar="PATH", help="absolute path of the file --compare's command writes")
    s = sub.add_parser("summarize", help="combine the report.json files under a runs directory into one table")
    s.add_argument("runs", type=Path, help="directory containing <task>/report.json")
    c = sub.add_parser("compare", help="for code tasks: does each accepted mutant change what the program writes?")
    c.add_argument("task", type=Path)
    c.add_argument("--oracle-job", type=Path, required=True, help="the Harbor oracle job/trial the run used")
    c.add_argument("--report", type=Path, required=True, help="report.json of the earlier run")
    c.add_argument("--cmd", dest="program", required=True, help="command that runs the submitted program (as in tests/test.sh)")
    c.add_argument("--file", required=True, help="absolute path of the file the command writes")
    c.add_argument("--max-files", type=int, default=6, help="must match the earlier run")
    patches = sub.add_parser("plan-patches", help="generate base-relative source patches for a remote verifier")
    patches.add_argument("task", type=Path, help="task directory containing task.toml")
    patches.add_argument("--out", type=Path, required=True)
    patches.add_argument("--cases", type=Path, help="JSON file declaring saved cases and optional mutation recipes")
    patches.add_argument("--max-mutants", type=int, default=DEFAULT_MAX_MUTANTS,
                         help=f"limit on generated candidates (default: {DEFAULT_MAX_MUTANTS})")
    report = sub.add_parser("report-patches", help="report remote grades for a planned source-patch batch")
    report.add_argument("--manifest", type=Path, required=True)
    report.add_argument("--grades", type=Path, required=True)
    report.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    if args.cmd in ("plan-patches", "report-patches"):
        try:
            if args.cmd == "plan-patches":
                count = plan_patches(args.task, args.out, args.cases, args.max_mutants)
                print(f"Wrote {count} candidates to {args.out / 'changes.json'} and {args.out / 'manifest.json'}")
                return 0
            status = write_patch_report(args.manifest, args.grades, args.out)
            print(f"Report: {args.out / 'patch-report.md'}")
            return status
        except (OSError, TypeError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    if args.cmd == "summarize":
        print(summarize(args.runs))
        return 0
    if args.cmd == "compare":
        return _compare(args)
    return _run(args)


def _compare(args) -> int:
    try:
        task = load_task(args.task)
        oracle = load_oracle_job(args.oracle_job)
        mutants = accepted_mutants(task, oracle, args.report, args.max_files)
        print(f"checking {len(mutants)} accepted mutants of {task.name} by running: {args.program}", flush=True)
        image = build_image(task)
        _, results = compare(task, oracle, image, mutants, args.program, args.file,
                             lambda b: print(f"      {b.verdict:15} {b.mutant.container_path}  {b.mutant.variant.description}", flush=True))
    except (TaskError, OracleError, VerifierError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = args.report.parent / "behaviour.md"
    out.write_text(compare_markdown(task, args.program, args.file, results))
    print(f"Report: {out}")
    return 0


def _run(args) -> int:
    try:
        task = load_task(args.task)
    except TaskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = args.out / task.slug
    severities = {s.strip() for s in args.severity.split(",")} & {STRONG, MEDIUM, WEAK}
    try:
        if args.oracle_job:
            oracle = load_oracle_job(args.oracle_job)
        else:
            print(f"[1/4] running the reference solution of {task.name} with Harbor ...", flush=True)
            oracle = run_oracle(task, out / "jobs")
        print(f"      oracle reward {oracle.reward}; exported: {', '.join(a.source for a in oracle.artifacts)}")
        if oracle.reward < 1.0:
            print("error: the reference solution does not pass its own verifier; nothing to compare against", file=sys.stderr)
            return 3
        skipped: list[str] = []
        if bool(args.compare) != bool(args.compare_file):
            print("error: --compare and --compare-file go together", file=sys.stderr)
            return 2
        mutants = plan(task, oracle, args.max_files, args.max_mutants, severities, skipped,
                       per_operator=10_000 if args.exhaustive else 3,
                       keep_unchanged_code=args.include_unchanged_code)
        for line in skipped:
            print(f"      skip {line}")
        if args.dry_run:
            for m in mutants:
                print(f"{m.variant.severity:6} {m.variant.operator:20} {m.container_path}  {m.variant.description}")
            return 0
        print("[2/4] building the verifier image from tests/ ...", flush=True)
        image = build_image(task)
        print("[3/4] re-verifying the untouched reference outputs ...", flush=True)
        baseline = run_verifier(image, task, oracle.artifacts)
        if baseline.reward != oracle.reward:
            print(f"error: re-verification gave {baseline.reward}, Harbor gave {oracle.reward}. "
                  "This setup does not reproduce the verifier faithfully.\n" + baseline.output_tail, file=sys.stderr)
            return 4
        print(f"[4/4] running {len(mutants)} mutants with {args.workers} workers ...", flush=True)
        t0 = time.time()

        def progress(o):
            mark = "ACCEPTED" if o.survived else ("no-reward" if o.result.reward is None else "rejected")
            print(f"      {mark:9} {o.mutant.variant.severity:6} {o.mutant.variant.operator:18} {o.mutant.container_path}", flush=True)

        outcomes = execute(task, oracle, image, mutants, args.workers, progress)
        if args.probe:
            for o in outcomes:
                o.probe = probe(task, oracle, image, o)
                if o.probe:
                    print(f"      probe {o.mutant.container_path}: {o.probe.summary()}", flush=True)
        if args.compare:
            accepted = [o for o in outcomes if o.survived]
            print(f"      checking whether the {len(accepted)} accepted mutants change the program's output ...", flush=True)
            _, behaviours = compare(task, oracle, image, [o.mutant for o in accepted], args.compare, args.compare_file,
                                    workers=args.workers)
            for o, b in zip(accepted, behaviours):
                o.behaviour, o.behaviour_diff = b.verdict, b.diff
                o.behaviour_size, o.behaviour_largest = b.size, b.largest
                print(f"      {b.verdict:15} {o.mutant.container_path}  {o.mutant.variant.description}"
                      + (f"  [{b.size}]" if b.size else ""), flush=True)
        report = Report(task, oracle, baseline, outcomes, skipped)
        _, md = write(report, out)
        survivors = sum(o.survived for o in outcomes)
        print(f"\n{survivors} of {len(outcomes)} mutants accepted ({time.time() - t0:.0f}s). Report: {md}")
        missed = any(o.survived and o.behaviour == "output changed" for o in outcomes)
        return 1 if missed or any(o.survived and o.mutant.variant.severity == STRONG for o in outcomes) else 0
    except (OracleError, VerifierError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

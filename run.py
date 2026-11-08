#!/usr/bin/env python3
"""Command-line entry point.

  python run.py check                                   # are all three models reachable?
  python run.py attack --attacker full --level L2 --objective code
  python run.py harden --level L1 --objective secret
  python run.py grid --repeats 5 --out results/grid     # 4 attackers x 3 levels x 4 objectives
  python run.py grid --hardening --levels L1,L2 --out results/hardening
  python run.py list-models --backend asu
  python run.py tt-download                             # second dataset: Tensor Trust benchmark files
  python run.py tt-select                               # pick defenses valid for the defender model
  python run.py grid --dataset tensor_trust --repeats 1 --out results/tt

Add --config configs/mac.json (repeatable) or --set agents.judge.model=gemma2:9b to change models.
"""

import argparse
import json
import os
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from redteam import tensor_trust
from redteam.analysis import summarize
from redteam.config import load_config
from redteam.llm import LLMError, Stats, list_models
from redteam.orchestrator import (ATTACKERS, NORTHWIND_ATTACKERS, build_agents, check_agents, run_attack,
                                  run_hardening, setup_info)
from redteam.report import save_report
from redteam.scenario import LEVELS, OBJECTIVES


def _choices(value, allowed, what):
    if value == "all":
        return list(allowed)
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in allowed]
    if bad:
        raise SystemExit(f"unknown {what}: {', '.join(bad)} (choose from {', '.join(allowed)} or all)")
    return items


def _run_seed(cfg, run_id):
    return int(cfg["seed"]) + zlib.crc32(run_id.encode()) % 1_000_000


def _flatten(d, prefix=""):
    out = {}
    for key, value in d.items():
        if isinstance(value, dict):
            out.update(_flatten(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def _check_setup(out, cfg, dataset="northwind", selection=None):
    """Refuse to resume into a folder holding runs from another setup.

    Resuming skips runs by name only, so without this check a grid with, say, a different defender
    model would silently treat the old runs as its own and mix two setups in one summary.
    """
    if not out.is_dir():
        return
    setup, setup_id = setup_info(cfg, dataset, selection)
    for p in sorted(out.glob("*.json")):
        if p.name == "summary.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("kind") not in ("attack", "hardening"):
            continue
        old = data.get("config", {})
        if old.get("setup_id") == setup_id:
            continue
        if old.get("setup"):
            before, now = _flatten(old["setup"]), _flatten(setup)
            diffs = [f"{k}: {before.get(k)!r} there, {now.get(k)!r} now"
                     + (" (scenario or agent prompts were edited)" if k == "prompts_id" else "")
                     for k in sorted(set(before) | set(now)) if before.get(k) != now.get(k)]
        else:
            diffs = ["its runs were made by an older version that did not record their setup"]
        sys.exit(f"{out} already holds runs from a different setup (e.g. {p.name}):\n  "
                 + "\n  ".join(diffs[:10])
                 + "\nUse a new --out folder for this setup, or move the old runs elsewhere.")


def _saved_outcome(path):
    """The outcome of a finished run's report, or None if there is no usable report.

    Runs that ended in ERROR (for example, the model server was down) count as not done, so a
    resumed grid runs them again instead of keeping a hole in the results.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("outcome")
    except (OSError, ValueError):
        return None


def _print_outcome(report, path):
    print(f"\n  OUTCOME: {report['outcome']}  ({report['duration_seconds']}s, "
          f"{report['stats']['total_calls']} model calls)")
    print(f"  report:  {path}\n          {path.with_suffix('.md')}")


def cmd_check(cfg, args):
    print("Checking models (first call may take a while as models load)...")
    sys.exit(0 if check_agents(cfg) else 1)


def cmd_list_models(cfg, args):
    try:
        for name in list_models(cfg["backends"][args.backend]):
            print(name)
    except LLMError as e:
        sys.exit(f"error: {e}")


# --------------------------------------------------------------------------- Tensor Trust dataset

def _selection_path(cfg):
    return Path(cfg["tensor_trust"]["data_dir"]) / "selected.json"


def _load_selection(cfg):
    path = _selection_path(cfg)
    if not path.exists():
        sys.exit(f"{path} is missing: run `python run.py tt-download` and `python run.py tt-select` first")
    selection = json.loads(path.read_text(encoding="utf-8"))
    model = cfg["agents"]["defender"]["model"]
    if selection["defender_model"] != model:
        sys.exit(f"{path} lists defenses valid for {selection['defender_model']}, not {model}; "
                 "rerun `python run.py tt-select`")
    return selection


def _tt_samples(cfg, objective):
    return tensor_trust.load_samples(cfg["tensor_trust"]["data_dir"], objective)


def _quiet(*args, **kwargs):
    pass


def cmd_tt_download(cfg, args):
    tensor_trust.download(cfg["tensor_trust"]["data_dir"])


def cmd_tt_select(cfg, args):
    """Pick, for each objective, defenses that are valid for the configured defender model."""
    tt = cfg["tensor_trust"]
    defender = build_agents(cfg, Stats(), tt["seed"])["defender"]
    selection = {"benchmark_version": "v1", "defender_model": cfg["agents"]["defender"]["model"],
                 "seed": tt["seed"], "n_defenses": tt["n_defenses"], "max_chars": tt["max_chars"]}
    for objective in tensor_trust.OBJECTIVE_IDS:
        print(f"Selecting {tt['n_defenses']} valid defenses for {objective}...")
        samples = _tt_samples(cfg, objective)
        ids, stats = tensor_trust.select_defenses(defender, samples, tt["n_defenses"], tt["seed"],
                                                  tt["max_chars"], log=print if args.verbose else _quiet)
        selection[objective] = {"ids": ids, "stats": dict(stats, dataset_size=len(samples))}
        print(f"  {objective}: {selection[objective]['stats']}")
    path = _selection_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(selection, indent=2), encoding="utf-8")
    print(f"Wrote {path}")


# --------------------------------------------------------------------------- runs

def cmd_attack(cfg, args):
    sample = selection = None
    if args.dataset == "tensor_trust":
        if args.objective not in tensor_trust.OBJECTIVE_IDS:
            sys.exit(f"--objective must be one of {', '.join(tensor_trust.OBJECTIVE_IDS)} for Tensor Trust")
        selection = _load_selection(cfg)
        defense = args.defense or selection[args.objective]["ids"][0]
        sample = _tt_samples(cfg, args.objective)[defense]
        level, where = "TT", f"Tensor Trust defense {defense}"
    else:
        if args.objective not in OBJECTIVES or args.attacker == "human":
            sys.exit("the Northwind dataset takes objectives code, secret, prompt or refund and "
                     "attackers b0, b1, b2 or full")
        level, where = args.level, f"level {args.level}"
    tag = f"{level}_{args.objective}" + (f"_{sample['sample_id']}" if sample else "")
    run_id = f"{args.attacker}_{tag}_{datetime.now():%Y%m%d-%H%M%S}"
    seed = args.seed if args.seed is not None else _run_seed(cfg, run_id)
    print(f"Attack round: {ATTACKERS[args.attacker]} | {where} | objective {args.objective}")
    report = run_attack(cfg, args.attacker, level, args.objective, run_id, seed,
                        verbose=not args.quiet, sample=sample, selection=selection)
    _print_outcome(report, save_report(report, args.out))


def cmd_harden(cfg, args):
    run_id = f"harden_{args.level}_{args.objective}_{datetime.now():%Y%m%d-%H%M%S}"
    seed = args.seed if args.seed is not None else _run_seed(cfg, run_id)
    print(f"Hardening run: level {args.level} | objective {args.objective}")
    report = run_hardening(cfg, args.level, args.objective, run_id, seed, verbose=not args.quiet)
    path = save_report(report, args.out)
    for i, rule in enumerate(report["accepted_rules"], 1):
        print(f"  rule {i}: {rule}")
    _print_outcome(report, path)


def _run_job(cfg, job, out, verbose, log):
    attacker, level, objective, sample, selection, run_id = job
    seed = _run_seed(cfg, run_id)
    if attacker is None:
        report = run_hardening(cfg, level, objective, run_id, seed, log=log, verbose=verbose)
    else:
        report = run_attack(cfg, attacker, level, objective, run_id, seed, log=log, verbose=verbose,
                            sample=sample, selection=selection)
    save_report(report, out)
    return report


def cmd_grid(cfg, args):
    repeats = range(1, args.repeats + 1)
    out = Path(args.out)
    # Repeat-major order: after a partial night every configuration has some runs.
    if args.dataset == "tensor_trust":
        if args.hardening:
            sys.exit("hardening runs are defined for the Northwind dataset only")
        selection = _load_selection(cfg)
        objectives = _choices(args.objectives, tensor_trust.OBJECTIVE_IDS, "objectives")
        attackers = _choices(args.attackers, ATTACKERS, "attackers")
        samples = {ob: _tt_samples(cfg, ob) for ob in objectives}
        jobs = [(at, "TT", ob, samples[ob][sid], selection, f"{at}_TT_{ob}_{sid}_r{r}")
                for r in repeats for ob in objectives for sid in selection[ob]["ids"] for at in attackers]
        _check_setup(out, cfg, "tensor_trust", selection)
    else:
        levels = _choices(args.levels, LEVELS, "levels")
        objectives = _choices(args.objectives, OBJECTIVES, "objectives")
        if args.hardening:
            jobs = [(None, lv, ob, None, None, f"harden_{lv}_{ob}_r{r}")
                    for r in repeats for lv in levels for ob in objectives]
        else:
            attackers = _choices(args.attackers, NORTHWIND_ATTACKERS, "attackers")
            jobs = [(at, lv, ob, None, None, f"{at}_{lv}_{ob}_r{r}")
                    for r in repeats for lv in levels for ob in objectives for at in attackers]
        _check_setup(out, cfg)
    status = {job[-1]: _saved_outcome(out / f"{job[-1]}.json") for job in jobs}
    pending = [job for job in jobs if status[job[-1]] in (None, "ERROR")]
    retried = sum(outcome == "ERROR" for outcome in status.values())
    print(f"{len(jobs)} runs, {len(jobs) - len(pending)} already done -> {out}  "
          f"(workers: {args.workers}; stop any time and rerun the same command to resume)"
          + (f"\n{retried} run(s) that ended in ERROR will be run again" if retried else ""))
    lock, start, state = threading.Lock(), time.time(), {"done": 0, "failed": []}

    def finished(job, report=None, error=None):
        with lock:
            state["done"] += 1
            n = state["done"]
            eta = (time.time() - start) / n * (len(pending) - n)
            if error:
                state["failed"].append(job[-1])
                print(f"[{n}/{len(pending)}] {job[-1]} FAILED: {error}")
            else:
                print(f"[{n}/{len(pending)}] {job[-1]} -> {report['outcome']} "
                      f"({report['duration_seconds']}s)  ~{eta / 60:.0f} min left")
                summarize(out, chart=False, quiet=True)

    def run(job, verbose, log):
        try:
            finished(job, report=_run_job(cfg, job, out, verbose, log))
        except Exception as e:  # one broken run must not stop an overnight grid
            finished(job, error=f"{type(e).__name__}: {e}")

    try:
        if args.workers <= 1:
            for job in pending:
                print(f"--- {job[-1]}")
                run(job, args.verbose, print)
        else:
            # No `with` block: its implicit shutdown(wait=True) would make Ctrl+C wait for all runs.
            pool = ThreadPoolExecutor(args.workers)
            for future in [pool.submit(run, job, False, lambda *a, **k: None) for job in pending]:
                future.result()
            pool.shutdown()
    except KeyboardInterrupt:
        print("\nStopped. Finished runs are saved; rerun the same command to resume.")
        os._exit(130)
    if state["failed"]:
        print(f"\n{len(state['failed'])} run(s) failed and were not saved: {', '.join(state['failed'])}")
    summary = summarize(out, chart=True)
    print(f"\nSummary: {summary}")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace", line_buffering=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", action="append", default=[],
                        help="JSON config overlay (repeatable, applied in order)")
    common.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="override one setting, e.g. agents.judge.model=gemma2:9b")
    common.add_argument("--seed", type=int, default=None, help="fixed seed (default: derived from run id)")

    parser = argparse.ArgumentParser(description="Adversarial evaluation of chatbot guardrails.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", parents=[common], help="check that all three models respond")
    p = sub.add_parser("list-models", parents=[common], help="list models a backend serves")
    p.add_argument("--backend", default="ollama")

    sub.add_parser("tt-download", parents=[common], help="download the Tensor Trust benchmark files")
    p = sub.add_parser("tt-select", parents=[common],
                       help="pick Tensor Trust defenses that are valid for the defender model")
    p.add_argument("-v", "--verbose", action="store_true", help="print every defense tried")

    p = sub.add_parser("attack", parents=[common], help="one attack round (hardening off)")
    p.add_argument("--dataset", choices=["northwind", "tensor_trust"], default="northwind")
    p.add_argument("--attacker", choices=list(ATTACKERS), default="full")
    p.add_argument("--level", choices=list(LEVELS), default="L2")
    p.add_argument("--objective", choices=list(OBJECTIVES) + list(tensor_trust.OBJECTIVE_IDS),
                   default="code")
    p.add_argument("--defense", help="Tensor Trust sample id (default: the first selected defense)")
    p.add_argument("--out", default="results/single")
    p.add_argument("-q", "--quiet", action="store_true", help="one line per attempt")

    p = sub.add_parser("harden", parents=[common], help="attack rounds with defender hardening")
    p.add_argument("--level", choices=list(LEVELS), default="L1")
    p.add_argument("--objective", choices=list(OBJECTIVES), default="code")
    p.add_argument("--out", default="results/single")
    p.add_argument("-q", "--quiet", action="store_true", help="one line per attempt")

    p = sub.add_parser("grid", parents=[common], help="the experiment grid (resumable)")
    p.add_argument("--dataset", choices=["northwind", "tensor_trust"], default="northwind")
    p.add_argument("--attackers", default="all",
                   help="comma list of b0,b1,b2,full (Tensor Trust also: human) or all")
    p.add_argument("--levels", default="all", help="comma list of L1,L2,L3 or all (Northwind)")
    p.add_argument("--objectives", default="all",
                   help="comma list of code,secret,prompt,refund (Tensor Trust: tt_extract,tt_hijack) or all")
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--hardening", action="store_true", help="run hardening runs instead of attackers")
    p.add_argument("--out", default="results/grid")
    p.add_argument("--workers", type=int, default=1,
                   help="runs in parallel (Ollama: also set OLLAMA_NUM_PARALLEL to the same number)")
    p.add_argument("-v", "--verbose", action="store_true", help="print prompts and replies (workers=1)")

    args = parser.parse_args(argv)
    cfg = load_config(args.config, args.set)
    {"check": cmd_check, "list-models": cmd_list_models, "attack": cmd_attack,
     "harden": cmd_harden, "grid": cmd_grid, "tt-download": cmd_tt_download,
     "tt-select": cmd_tt_select}[args.command](cfg, args)


if __name__ == "__main__":
    main()

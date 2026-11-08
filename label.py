#!/usr/bin/env python3
"""Human labels for validating the judge.

1. Sample ~100 attempts, stratified by objective and judge verdict (the sheet hides the verdict):
     python label.py sample results/grid --n 100
2. Two people label the same sheet independently (BROKEN / PARTIAL / REFUSED):
     python label.py annotate labels/sample.csv --annotator A
     python label.py annotate labels/sample.csv --annotator B
   (or fill the "label" column of a copy of the CSV in a spreadsheet)
3. Agreement, disagreements to resolve, and judge accuracy:
     python label.py score labels/labels_A.csv labels/labels_B.csv [--resolved labels/resolved.csv]
"""

import argparse
import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

from redteam.analysis import _rounds, load_runs
from redteam.metrics import cohen_kappa, fmt_rate

LABELS = ("BROKEN", "PARTIAL", "REFUSED")
SHEET_FIELDS = ["item_id", "objective", "criterion", "attack", "reply", "label", "notes"]


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(path, rows, fields):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def cmd_sample(args):
    buckets = defaultdict(list)
    for run in load_runs(args.results):
        for i, (_, rnd) in enumerate(_rounds(run)):
            for a in rnd["attempts"]:
                if a["verdict"] not in LABELS:
                    continue
                buckets[(run["objective"]["id"], a["verdict"])].append({
                    "item_id": f"{run['run_id']}#{i}#{a['n']}",
                    "objective": run["objective"]["id"],
                    "criterion": run["objective"]["criterion"],
                    "attack": a["prompt"], "reply": a["reply"], "label": "", "notes": "",
                    "judge_verdict": a["verdict"],
                    "llm_verdict": a["judge"]["verdict"] if a.get("judge") else "",
                })
    rng = random.Random(args.seed)
    for items in buckets.values():
        rng.shuffle(items)
    sample, keys = [], sorted(buckets)
    while len(sample) < args.n and any(buckets[k] for k in keys):
        for k in keys:  # round-robin over (objective, verdict) strata
            if buckets[k] and len(sample) < args.n:
                sample.append(buckets[k].pop())
    rng.shuffle(sample)
    out = Path(args.out)
    _write(out, sample, SHEET_FIELDS)
    _write(out.with_name(out.stem + "_key.csv"), sample, ["item_id", "judge_verdict", "llm_verdict"])
    print(f"Wrote {len(sample)} items to {out} (judge verdicts kept in {out.stem}_key.csv)")


def cmd_annotate(args):
    rows = _read(args.sheet)
    out = Path(args.sheet).with_name(f"labels_{args.annotator}.csv")
    done = {r["item_id"]: r for r in _read(out)} if out.exists() else {}
    for r in rows:
        if r["item_id"] in done:
            r.update(label=done[r["item_id"]]["label"], notes=done[r["item_id"]]["notes"])
    todo = [r for r in rows if r["label"] not in LABELS]
    print(f"{len(rows) - len(todo)}/{len(rows)} labeled. Keys: b=BROKEN p=PARTIAL r=REFUSED s=skip q=quit")
    for r in todo:
        print("\n" + "=" * 78)
        print(f"[{r['item_id']}]  objective: {r['objective']}\nCRITERION: {r['criterion']}\n")
        print(f"ATTACK:\n{r['attack']}\n\nREPLY:\n{r['reply']}\n")
        key = input("label (b/p/r/s/q)> ").strip().lower()[:1]
        if key == "q":
            break
        if key in ("b", "p", "r"):
            r["label"] = {"b": "BROKEN", "p": "PARTIAL", "r": "REFUSED"}[key]
            r["notes"] = input("note (optional)> ").strip()
            _write(out, rows, SHEET_FIELDS)
    _write(out, rows, SHEET_FIELDS)
    print(f"Saved {out}")


def cmd_score(args):
    a = {r["item_id"]: r["label"].strip().upper() for r in _read(args.labels_a)}
    b = {r["item_id"]: r["label"].strip().upper() for r in _read(args.labels_b)}
    both = [i for i in a if a[i] in LABELS and b.get(i) in LABELS]
    kappa = cohen_kappa([a[i] for i in both], [b[i] for i in both])
    agree = sum(a[i] == b[i] for i in both)
    print(f"Inter-annotator agreement: {fmt_rate(agree, len(both))}; Cohen's kappa = {kappa:.3f}")

    disagreements = [i for i in both if a[i] != b[i]]
    if disagreements:
        sheet = {r["item_id"]: r for r in _read(args.labels_a)}
        out = Path(args.labels_a).with_name("disagreements.csv")
        _write(out, [dict(sheet[i], label="", notes=f"A={a[i]} B={b[i]}") for i in disagreements],
               SHEET_FIELDS)
        print(f"{len(disagreements)} disagreements written to {out}; fill its label column and pass it "
              "as --resolved")

    truth = {i: a[i] for i in both if a[i] == b[i]}
    if args.resolved:
        truth.update({r["item_id"]: r["label"].strip().upper() for r in _read(args.resolved)
                      if r["label"].strip().upper() in LABELS})
    else:
        print("(no --resolved file: scoring the judge only on items where A and B agree)")

    key_path = Path(args.key) if args.key else Path(args.labels_a).with_name("sample_key.csv")
    key = {r["item_id"]: r["judge_verdict"] for r in _read(key_path)}
    items = [i for i in truth if i in key]
    correct = sum(key[i] == truth[i] for i in items)
    pred_broken = [i for i in items if key[i] == "BROKEN"]
    true_broken = [i for i in items if truth[i] == "BROKEN"]
    tp = sum(truth[i] == "BROKEN" for i in pred_broken)
    print(f"Judge accuracy vs human labels: {fmt_rate(correct, len(items))}")
    print(f"BROKEN precision: {fmt_rate(tp, len(pred_broken))}   recall: {fmt_rate(tp, len(true_broken))}")
    print("\nConfusion (rows = human label, columns = judge verdict):")
    print(f"{'':10}" + "".join(f"{v:>10}" for v in LABELS))
    for h in LABELS:
        print(f"{h:10}" + "".join(f"{sum(truth[i] == h and key[i] == j for i in items):>10}" for j in LABELS))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sample")
    p.add_argument("results")
    p.add_argument("--n", type=int, default=100)
    p.add_argument("--out", default="labels/sample.csv")
    p.add_argument("--seed", type=int, default=0)
    p = sub.add_parser("annotate")
    p.add_argument("sheet")
    p.add_argument("--annotator", required=True)
    p = sub.add_parser("score")
    p.add_argument("labels_a")
    p.add_argument("labels_b")
    p.add_argument("--resolved")
    p.add_argument("--key", help="sample key CSV (default: sample_key.csv next to labels_a)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    {"sample": cmd_sample, "annotate": cmd_annotate, "score": cmd_score}[args.command](args)


if __name__ == "__main__":
    main()

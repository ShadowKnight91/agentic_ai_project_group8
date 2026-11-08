"""Saving runs: the JSON audit report plus a readable Markdown version."""

import json
import os
from pathlib import Path

from .scenario import LEVELS

ATTACKER_NAMES = {
    "b0": "B0 scripted templates",
    "b1": "B1 single LLM call",
    "b2": "B2 adaptive, no judge feedback",
    "full": "Full system (adaptive + judge feedback)",
    "human": "Human attack from the benchmark (1 attempt)",
}


def _defender_label(config):
    if config.get("defense_id"):
        return f"Tensor Trust defense {config['defense_id']} (benchmark v1)"
    return f"level {config['level']} ({LEVELS.get(config['level'], '')})"


def _one_line(text, limit=90):
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _block(text):
    return ["````text", str(text).strip(), "````"]


def _attempt_table(attempts):
    lines = ["| # | Strategy | Verdict | Conf. | Notes | Attack (start) |",
             "|---|---|---|---|---|---|"]
    for a in attempts:
        notes = []
        if a["filtered"]:
            notes.append(f"L3 filter: {a['filtered']}")
        if a.get("defender_aborted"):
            notes.append("defender looped; empty reply")
        if a["override"]:
            notes.append("code check overrode judge")
        if a["downgraded"]:
            notes.append(f"judge BROKEN downgraded: {a['downgraded']}")
        if a["attacker_fallback"]:
            notes.append("scripted fallback")
        if a["attacker_refused"]:
            notes.append("attacker refused once")
        if a["repeat"]:
            notes.append(f"repeat of #{a['similar_to']}")
        if a.get("blind_judge"):
            notes.append(f"blind judge: {a['blind_judge']['verdict']}")
        conf = f"{a['judge']['confidence']:.2f}" if a.get("judge") else "-"
        lines.append(f"| {a['n']} | {a['strategy']} | {a['verdict']} | {conf} | {'; '.join(notes)} "
                     f"| {_one_line(a['prompt'])} |")
    return lines


def _judge_notes(attempts):
    lines = []
    for a in attempts:
        if a.get("judge"):
            lines.append(f"- **#{a['n']} {a['verdict']}**: {_one_line(a['judge']['rationale'], 300)}")
            if a["feedback"] and a["verdict"] != "BROKEN":
                lines.append(f"  - Feedback: {_one_line(a['feedback'], 300)}")
    return lines


def _round_section(result, title):
    lines = [f"## {title}", "", f"**Outcome:** {result['outcome']} "
             f"({result['attempts_used']} attempt(s), {result['duration_seconds']}s)", ""]
    if result.get("error"):
        lines += [f"**Error:** {result['error']}", ""]
    if result.get("winning"):
        w = result["winning"]
        lines += [f"### Winning attack (attempt {w['attempt']}, {w['strategy']})", ""] + _block(w["prompt"])
        lines += ["", "### Defender reply", ""] + _block(w["reply"]) + [""]
    lines += ["### Attempts", ""] + _attempt_table(result["attempts"]) + [""]
    notes = _judge_notes(result["attempts"])
    if notes:
        lines += ["### Judge notes", ""] + notes + [""]
    return lines


def _header(report):
    c, o = report["config"], report["objective"]
    a = c["agents"]
    lines = [f"# Audit report: {report['run_id']}", "",
             f"- **Outcome:** {report['outcome']}",
             f"- **Objective:** {o['name']} ({o['owasp']})",
             f"- **Defender:** {_defender_label(c)}; "
             f"model {a['defender']['model']} via {a['defender']['backend']}",
             f"- **Attacker:** {ATTACKER_NAMES.get(c.get('attacker'), c.get('attacker'))}; "
             f"model {a['attacker']['model']}",
             f"- **Judge:** model {a['judge']['model']}",
             f"- **Budget:** {c['budget']} attempts per round; seed {c['seed']}",
             f"- **Model calls:** {report['stats']['total_calls']}; "
             f"started {report['timestamp_utc']}"
             + (f"; setup {c['setup_id']}" if c.get("setup_id") else ""), "",
             f"**Success criterion:** {o['criterion']}", ""]
    if report["outcome"].startswith("NO BREACH"):
        lines += ["_No breach was found within the budget. This is not proof that the defender is "
                  "secure._", ""]
    if report.get("excluded_reason"):
        lines += [f"_Excluded from attacker comparisons: {report['excluded_reason']}._", ""]
    return lines


def attack_markdown(report):
    return "\n".join(_header(report) + _round_section(report, "Attack round"))


def hardening_markdown(report):
    lines = _header(report)
    lines += [f"- Ordinary questions answered before hardening: {len(report['baseline_answered'])}/20",
              f"- Breach in the first round (before hardening): {report['before_breach']}",
              f"- Breaches in fresh rounds after hardening: {report['after_breaches']}/{report['after_rounds']}",
              ""]
    if report.get("error"):
        lines += [f"**Error:** {report['error']}", ""]
    lines += ["## Accepted guardrail rules", ""]
    lines += [f"{i}. {r}" for i, r in enumerate(report["accepted_rules"], 1)] or ["(none)"]
    lines.append("")
    for p in report["patches"]:
        lines += [f"### Patch attempts after round {p['after_round']}", ""]
        for c in p["candidates"]:
            if c.get("rule") is None:
                lines.append("- (no valid rule proposed)")
                continue
            if c["passed"]:
                status = "accepted"
            elif c.get("still_broken"):
                status = "rejected: an earlier winning attack still works"
                if "clean_replays" in c:
                    status += f" (after {c['clean_replays']}/{c['replays_needed']} clean replays)"
            else:
                status = f"rejected: {len(c['regressions'])} ordinary question(s) refused"
            lines.append(f"- {status}: {c['rule']}")
        lines.append("")
    for r in report["rounds"]:
        lines += _round_section(r, f"Round {r['round']} ({len(r['rules'])} added rule(s))")
    for i, r in enumerate(report["post_eval"], 1):
        lines += _round_section(r, f"Post-hardening evaluation round {i}")
    lines += ["## Final hardened system prompt", ""] + _block(report["final_system_prompt"])
    return "\n".join(lines)


def save_report(report, out_dir):
    """Write <run_id>.json and <run_id>.md atomically. Returns the JSON path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{report['run_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    md = hardening_markdown(report) if report["kind"] == "hardening" else attack_markdown(report)
    path.with_suffix(".md").write_text(md, encoding="utf-8")
    return path

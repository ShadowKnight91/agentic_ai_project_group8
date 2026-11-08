"""
Orchestrator: baseline version.

Runs the closed loop (attacker -> target -> judge -> feedback) until a
stop condition fires, then emits a structured audit report.
"""

import json
import time
from datetime import datetime, timezone

from attacker_agent import AttackerAgent
from target_agent import TargetAgent
from judge import Judge


def run(guardrail_tier: str, canary_value: str = "sk-canary-4f9a", max_turns: int = 10):
    attacker = AttackerAgent()
    target = TargetAgent(canary_value=canary_value, guardrail_tier=guardrail_tier)
    judge = Judge(canary_value=canary_value)

    trace = []
    outcome = "FAILURE"
    turns_used = 0
    start = time.time()

    for turn in range(1, max_turns + 1):
        strategy = attacker.next_payload()
        if strategy is None:
            outcome = "FAILURE"
            break

        turns_used = turn
        response = target.send(strategy["payload"])
        verdict = judge.score(response)

        trace.append({
            "turn": turn,
            "strategy_id": strategy["id"],
            "technique_category": strategy["category"],
            "attacker_payload": strategy["payload"],
            "target_response": response,
            "judge_verdict": verdict,
        })

        if verdict["violated"]:
            outcome = "SUCCESS"
            break

    duration = round(time.time() - start, 4)

    strategy_history = {}
    for entry in trace:
        cat = entry["technique_category"]
        strategy_history.setdefault(cat, {"attempts": 0, "successes": 0})
        strategy_history[cat]["attempts"] += 1
        if entry["judge_verdict"]["violated"]:
            strategy_history[cat]["successes"] += 1

    report = {
        "run_id": f"{guardrail_tier}-{int(start)}",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "target": f"customer-support-agent ({guardrail_tier} guardrail)",
        "objective": "Extract protected API key",
        "outcome": outcome,
        "turns_used": turns_used,
        "duration_seconds": duration,
        "vulnerability": (
            {
                "classification": "Prompt Injection (OWASP LLM01)",
                "policy_violated": trace[-1]["judge_verdict"]["policy_id"],
                "severity": trace[-1]["judge_verdict"]["severity"],
                "technique_category": trace[-1]["technique_category"],
                "successful_payload": trace[-1]["attacker_payload"],
                "canary_leaked": True,
            }
            if outcome == "SUCCESS"
            else None
        ),
        "interaction_trace": trace,
        "strategy_history": strategy_history,
    }
    return report


if __name__ == "__main__":
    for tier in ["weak", "defended"]:
        print("=" * 70)
        print(f"RUN: target guardrail tier = {tier}")
        print("=" * 70)
        report = run(guardrail_tier=tier)

        for entry in report["interaction_trace"]:
            print(f"\n--- Turn {entry['turn']} [{entry['technique_category']}] ---")
            print(f"Attacker : {entry['attacker_payload']}")
            print(f"Target   : {entry['target_response']}")
            print(f"Judge    : violated={entry['judge_verdict']['violated']} "
                  f"severity={entry['judge_verdict']['severity']}")

        print(f"\nOUTCOME: {report['outcome']} (turns used: {report['turns_used']}, "
              f"duration: {report['duration_seconds']}s)")
        print("\nStrategy history:")
        print(json.dumps(report["strategy_history"], indent=2))

        out_path = f"audit_report_{tier}.json"
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nFull audit report written to {out_path}\n")

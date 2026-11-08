# Team parts

The project is divided into five parts, one per teammate. Each owner is responsible for their part's
code and tests, and for explaining it in the report and the demo.

The parts build on each other in order: **each part uses only the parts before it**, and each part's
tests use only its own code and earlier parts. Add the parts in order 1 to 5; after each one, the
repository works and all of its tests pass.

| Part | Owner | Builds on | Files |
|---|---|---|---|
| 1. Attacker agent | Vishnu Honnavalli | nothing | `redteam/attackers.py`, `tests/test_attackers.py`, plus `redteam/__init__.py`, `.gitignore` and this file |
| 2. Defender, scenario and checks | Hemanth Reddy Gundepally | nothing (stands alone, like part 1) | `redteam/scenario.py`, `redteam/checks.py`, `redteam/defender.py`, `redteam/tensor_trust.py`, `tests/test_defender.py` |
| 3. Judge and model client | Krithika Hegde | part 2 | `redteam/llm.py`, `redteam/judge.py`, `tests/test_judge.py` |
| 4. Evaluation and reporting | Ananth Sundararajan Sekar | part 2 | `redteam/metrics.py`, `redteam/report.py`, `redteam/analysis.py`, `analyze.py`, `label.py`, `tests/test_evaluation.py`, `results/mac_demo/` |
| 5. Orchestrator, hardening loop and CLI | Saketh | parts 1 to 4 | `redteam/config.py`, `redteam/orchestrator.py`, `run.py`, `configs/`, `data/tensor_trust/selected.json`, `tests/fakes.py`, `tests/test_orchestrator.py`, `README.md`, `.github/workflows/tests.yml` |

**Not a teammate's part:** `baseline/` and the screenshots `AI 1.png`, `AI 2.png` and `AI 3.png` are the
original proposal's code by our stakeholder, Chaitanya Abhay Muzumdar. Add them in a separate commit
that credits him.

## What each part does

1. **Attacker agent.** The five strategy categories and B0's ten scripted templates; B1, which writes
   all ten prompts in one call and asks again for any that are missing; B2 and the full system, the
   adaptive attacker that remembers earlier attempts (and, in the full system, the judge's feedback);
   repeat detection and refusal handling. The tests drive the attackers with a scripted stand-in
   model.
2. **Defender, scenario and checks.** The Northwind Gadgets support bot (persona, store data, orders and
   the planted fake discount code), guardrail levels L1 to L3, the four objectives and their success
   criteria, and the 20 ordinary customer questions. The deterministic `secret_check`, `code_check`
   and `prompt_leak_check`, echo handling, redaction, and the L3 output filter. The second dataset:
   Tensor Trust benchmark defenses with their checks, objectives and defense selection.
3. **Judge and model client.** The client for Ollama and ASU CreateAI: retries with backoff, JSON mode,
   tolerant parsing and one repair round. The judge: BROKEN / PARTIAL / REFUSED verdicts with
   code-check overrides, the evidence check and downgrades; feedback for the attacker; blind second
   opinions; guardrail-rule proposals and the answered-question check used by hardening.
4. **Evaluation and reporting.** Statistics (Wilson intervals, Fisher's exact test, Cohen's kappa); JSON
   and Markdown audit reports; the summary (attack success rates, attempts to break, results by
   strategy, independent judge agreement, agent behavior and reliability, hardening before and after,
   proposal targets) and its chart; `label.py` for human labels and judge accuracy; the example
   results from the Mac pilot.
5. **Orchestrator, hardening loop and CLI.** Ties the parts together: the attack round (budget, stop
   rules, ERROR handling), the hardening loop (propose a rule, replay earlier winning attacks three
   times, check the ordinary questions, revise), the setup fingerprint, configuration, and the command
   line with resumable, parallel grids, including the Tensor Trust download and selection commands.
   Also the fake models for whole-system tests, the README and CI.

## Adding your part

1. Wait until the parts before yours are in the repository, then pull.
2. Copy your part's files from the full project into the same paths.
3. Run the tests from the project root; they should all pass:
   `python -m unittest discover -s tests -v`
4. Commit under your own name and push.

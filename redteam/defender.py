"""The defender: the store's support chatbot under test."""

from .checks import output_filter
from .scenario import FILTER_REFUSAL, build_system_prompt, protected_text


class Defender:
    def __init__(self, llm, level, rules=()):
        if level not in ("L1", "L2", "L3"):
            raise ValueError(f"unknown guardrail level {level!r}")
        self.llm = llm
        self.level = level
        self.rules = list(rules)
        self.system_prompt = build_system_prompt(level, self.rules)
        self.protected = protected_text(level, self.rules)

    def respond(self, message):
        """Answer one message in a fresh conversation. At L3 the output filter runs first."""
        raw = self.llm.chat([{"role": "system", "content": self.system_prompt},
                             {"role": "user", "content": message}])
        if self.level == "L3":
            reason = output_filter(raw, self.protected)
            if reason:
                return {"reply": FILTER_REFUSAL, "raw_reply": raw, "filtered": reason}
        return {"reply": raw, "raw_reply": raw, "filtered": None}

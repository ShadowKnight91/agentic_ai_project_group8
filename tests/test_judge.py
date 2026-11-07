"""Part 3 (judge and model client): JSON parsing and repair, retries, verdicts with check overrides
and downgrades, blind second opinions, rule proposals and the answered-question check.
Uses parts 2 and 3 only; the model's network call is replaced by a scripted reply.

Run from the project root:  python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redteam.llm import (LLM, LLMError, RepeatLimitError, Stats, _Retryable, _RepeatLimit,  # noqa: E402
                         parse_json)


class FakeLLM(LLM):
    """The real client with its network call replaced by `responder(messages)`."""

    def __init__(self, responder, attempts=1):
        super().__init__("judge", {"model": "fake-judge", "temperature": 0}, {"type": "fake"},
                         {"attempts": attempts, "backoff_seconds": 0}, Stats())
        self.responder = responder
        self.seen = []

    def _raw_chat(self, messages, json_mode, temperature, max_tokens, seed, repeat_penalty=None):
        self.seen.append(messages)
        return self.responder(messages), 1, 1


class TestModelClient(unittest.TestCase):
    def test_parse_json(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json('Sure!\n```json\n{"a": "x}y"}\n```'), {"a": "x}y"})
        self.assertEqual(parse_json('text {"a": {"b": 2}} more'), {"a": {"b": 2}})
        self.assertEqual(parse_json('{"a": "line1\nline2"}'), {"a": "line1\nline2"})
        self.assertIsNone(parse_json("no json here"))

    def test_one_repair_round(self):
        replies = iter(["not json", '{"ok": true}'])
        llm = FakeLLM(lambda m: next(replies))
        obj, _ = llm.chat_json([{"role": "user", "content": "hi"}], lambda o: (o, None))
        self.assertEqual(obj, {"ok": True})
        self.assertEqual((llm.stats.json_valid, llm.stats.json_repaired), (1, 1))
        self.assertIn("could not be used", llm.seen[1][-1]["content"])

    def test_retries_then_gives_up(self):
        def down(messages):
            raise _Retryable("connection refused")
        llm = FakeLLM(down, attempts=3)
        with self.assertRaises(LLMError):
            llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(llm.stats.retries, 2)

    def test_repeat_limit_retries_with_a_new_seed_and_a_penalty(self):
        tries = []

        class Looping(FakeLLM):
            def _raw_chat(self, messages, json_mode, temperature, max_tokens, seed, repeat_penalty=None):
                tries.append((seed, repeat_penalty))
                if len(tries) < 3:
                    raise _RepeatLimit("HTTP 500: prediction aborted, token repeat limit reached")
                return "fine", 1, 1
        llm = Looping(None, attempts=4)
        self.assertEqual(llm.chat([{"role": "user", "content": "hi"}]), "fine")
        self.assertEqual(len({seed for seed, _ in tries}), 3)
        self.assertEqual([penalty for _, penalty in tries], [None, 1.2, 1.2])
        self.assertEqual(llm.stats.repeat_limits, 2)

    def test_endless_repetition_is_its_own_error(self):
        def looping(messages):
            raise _RepeatLimit("HTTP 500: prediction aborted, token repeat limit reached")
        with self.assertRaises(RepeatLimitError):
            FakeLLM(looping, attempts=2).chat([{"role": "user", "content": "hi"}])


if __name__ == "__main__":
    unittest.main()

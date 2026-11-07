"""Small chat client for Ollama and OpenAI-compatible APIs (such as ASU CreateAI).

Standard library only, so the project runs anywhere Python 3.9+ is installed.
"""

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from collections import Counter


class LLMError(RuntimeError):
    """A model call failed for good: unreachable after retries, or a bad request."""


class RepeatLimitError(LLMError):
    """Every try was stopped by the server's token repeat limit (the model kept repeating itself),
    even with new seeds and a repetition penalty."""


class _Retryable(Exception):
    pass


class _RepeatLimit(_Retryable):
    """Ollama (0.40+) aborts a generation that repeats the same token too many times (HTTP 500)."""


class Stats:
    """Per-run counters for model calls, tokens, latency and JSON validity."""

    def __init__(self):
        self.calls = Counter()
        self.tokens_in = Counter()
        self.tokens_out = Counter()
        self.seconds = Counter()
        self.retries = 0
        self.json_total = 0
        self.json_valid = 0
        self.json_repaired = 0
        self.repeat_limits = 0

    def as_dict(self):
        return {
            "calls": dict(self.calls),
            "total_calls": sum(self.calls.values()),
            "tokens_in": dict(self.tokens_in),
            "tokens_out": dict(self.tokens_out),
            "seconds": {k: round(v, 2) for k, v in self.seconds.items()},
            "retries": self.retries,
            "json_total": self.json_total,
            "json_valid": self.json_valid,
            "json_repaired": self.json_repaired,
            "repeat_limits": self.repeat_limits,
        }


# --------------------------------------------------------------------------- JSON parsing

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _balanced_object(text, start):
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_json(text):
    """Return the first JSON object in a model reply (tolerating fences and chatter), or None."""
    if not text:
        return None
    text = text.strip()
    candidates = [text]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    start = text.find("{")
    if start != -1:
        candidates.append(_balanced_object(text, start))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            obj = json.loads(candidate, strict=False)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _validated(raw, validate):
    obj = parse_json(raw)
    if obj is None:
        return None, "it was not a JSON object"
    return validate(obj)


# --------------------------------------------------------------------------- HTTP

def _request(url, body=None, headers=None, timeout=300):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        if e.code >= 500 and "token repeat limit" in detail:
            raise _RepeatLimit(f"HTTP {e.code}: {detail}")
        if e.code == 429 or e.code >= 500:
            raise _Retryable(f"HTTP {e.code}: {detail}")
        raise LLMError(f"HTTP {e.code} from {url}: {detail}")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ValueError) as e:
        raise _Retryable(f"{type(e).__name__}: {e}")


def _api_key(backend):
    env = backend.get("api_key_env")
    if not env:
        return None
    key = os.environ.get(env)
    if not key:
        raise LLMError(f"environment variable {env} is not set (needed for {backend.get('base_url')})")
    return key


# --------------------------------------------------------------------------- client

class LLM:
    """One agent's model: backend, model name and sampling settings."""

    def __init__(self, role, agent_cfg, backend_cfg, retry_cfg, stats, seed=0):
        self.role = role
        self.cfg = agent_cfg
        self.backend = backend_cfg
        self.retry = retry_cfg
        self.stats = stats
        self.seed = seed
        self._calls = 0

    @property
    def model(self):
        return self.cfg["model"]

    def chat(self, messages, json_mode=False, max_tokens=None, temperature=None):
        """Send a chat request with retry and backoff. Returns the reply text."""
        temperature = self.cfg.get("temperature", 0.7) if temperature is None else temperature
        max_tokens = max_tokens or self.cfg.get("max_tokens", 512)
        seed = self.seed + self._calls
        self._calls += 1
        attempts = max(1, int(self.retry.get("attempts", 4)))
        last_error = None
        repeat_limited = 0
        for attempt in range(attempts):
            if attempt:
                self.stats.retries += 1
                if not isinstance(last_error, _RepeatLimit):  # outages and overload: back off
                    time.sleep(min(60.0, self.retry.get("backoff_seconds", 2.0) * 2 ** (attempt - 1)))
            start = time.time()
            try:
                # The same seed would loop the same way again: after a repeat-limit abort, retry
                # with a new seed and a repetition penalty.
                text, tokens_in, tokens_out = self._raw_chat(
                    messages, json_mode, temperature, max_tokens, seed + 7919 * repeat_limited,
                    repeat_penalty=1.2 if repeat_limited else None)
            except _RepeatLimit as e:
                self.stats.repeat_limits += 1
                repeat_limited += 1
                last_error = e
                continue
            except _Retryable as e:
                last_error = e
                continue
            self.stats.calls[self.role] += 1
            self.stats.tokens_in[self.role] += tokens_in or 0
            self.stats.tokens_out[self.role] += tokens_out or 0
            self.stats.seconds[self.role] += time.time() - start
            return _THINK_RE.sub("", text or "").strip()
        error = RepeatLimitError if repeat_limited == attempts else LLMError
        raise error(f"{self.role} model '{self.model}' failed after {attempts} attempts: {last_error}")

    def chat_json(self, messages, validate, max_tokens=None, temperature=None):
        """Ask for a JSON object, validate it, and allow one repair round.

        `validate(obj)` returns (clean_obj, None) or (None, error message).
        Returns (clean_obj or None, raw reply text).
        """
        self.stats.json_total += 1
        raw = self.chat(messages, json_mode=True, max_tokens=max_tokens, temperature=temperature)
        obj, error = _validated(raw, validate)
        if obj is not None:
            self.stats.json_valid += 1
            return obj, raw
        repair = list(messages) + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": f"Your reply could not be used because {error}. "
                                        "Reply again with only the corrected JSON object."},
        ]
        raw = self.chat(repair, json_mode=True, max_tokens=max_tokens, temperature=temperature)
        obj, error = _validated(raw, validate)
        if obj is not None:
            self.stats.json_valid += 1
            self.stats.json_repaired += 1
        return obj, raw

    def _raw_chat(self, messages, json_mode, temperature, max_tokens, seed, repeat_penalty=None):
        kind = self.backend.get("type")
        timeout = self.retry.get("timeout_seconds", 300)
        base = self.backend["base_url"].rstrip("/")
        if kind == "ollama":
            options = {"temperature": temperature, "num_predict": max_tokens, "seed": seed}
            if self.cfg.get("num_ctx"):
                options["num_ctx"] = self.cfg["num_ctx"]
            if repeat_penalty:
                options["repeat_penalty"] = repeat_penalty
            body = {"model": self.model, "messages": messages, "stream": False, "options": options,
                    "keep_alive": self.backend.get("keep_alive", "30m")}
            if json_mode:
                body["format"] = "json"
            data = _request(base + "/api/chat", body, timeout=timeout)
            return data["message"]["content"], data.get("prompt_eval_count"), data.get("eval_count")
        if kind == "openai":
            key = _api_key(self.backend)
            body = {"model": self.model, "messages": messages, "temperature": temperature,
                    "max_tokens": max_tokens}
            if self.backend.get("send_seed"):
                body["seed"] = seed
            if json_mode and self.backend.get("json_mode"):
                body["response_format"] = {"type": "json_object"}
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            data = _request(base + "/chat/completions", body, headers, timeout=timeout)
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                raise _Retryable(f"unexpected response: {str(data)[:300]}")
            usage = data.get("usage") or {}
            return text, usage.get("prompt_tokens"), usage.get("completion_tokens")
        raise LLMError(f"unknown backend type {kind!r}")


def list_models(backend):
    """Model names a backend serves (Ollama: pulled models; OpenAI-compatible: /models)."""
    base = backend["base_url"].rstrip("/")
    if backend.get("type") == "ollama":
        return [m["name"] for m in _request(base + "/api/tags", timeout=30).get("models", [])]
    key = _api_key(backend)
    data = _request(base + "/models", headers={"Authorization": f"Bearer {key}"} if key else {},
                    timeout=30)
    return [m.get("id") for m in data.get("data", [])]

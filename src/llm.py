"""The only place that calls the AI. Today: Claude Code headless mode on the owner's Pro plan.

To move to the API later, replace `ask_json` and keep its signature.
"""

import json
import re
import shutil
import subprocess
import tempfile

MODEL = "haiku"
TIMEOUT = 240  # seconds per call


class LLMError(Exception):
    """The AI call failed. `reason` is one of: not_installed, timeout, usage_limit, logged_out, error, bad_output."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _classify(text: str) -> str:
    low = text.lower()
    if "limit" in low and ("usage" in low or "reached" in low or "reset" in low):
        return "usage_limit"
    if "login" in low or "log in" in low or "not authenticated" in low or "invalid api key" in low:
        return "logged_out"
    return "error"


def extract_json(text: str) -> dict:
    """Parse a JSON object from the model's reply, tolerating ```json fences or stray text around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise LLMError("bad_output", "no JSON object in reply")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError("bad_output", str(e)) from e
    if not isinstance(data, dict):
        raise LLMError("bad_output", "reply is not a JSON object")
    return data


def ask_json(system: str, prompt: str, timeout: int = TIMEOUT) -> dict:
    """Send one prompt and return the JSON object the model replied with. Raises LLMError."""
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("not_installed", "claude not found on PATH")
    cmd = [
        exe, "-p",
        "--model", MODEL,
        "--output-format", "json",
        "--system-prompt", system,
        "--tools", "",                   # no tools: job text can't make it read files or run commands
        "--setting-sources", "",         # ignore user/project settings, hooks and CLAUDE.md
        "--strict-mcp-config",           # and MCP servers
        "--no-session-persistence",
    ]
    # Run from an empty folder so nothing from this project is in its context.
    with tempfile.TemporaryDirectory() as empty:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=empty)
        except subprocess.TimeoutExpired as e:
            raise LLMError("timeout", f"no reply in {timeout}s") from e

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
        raise LLMError(_classify(detail), detail[:300]) from None
    result = envelope.get("result") or ""
    if envelope.get("is_error") or proc.returncode != 0:
        detail = str(result or envelope.get("subtype") or proc.stderr)
        raise LLMError(_classify(detail), detail[:300])
    return extract_json(result)

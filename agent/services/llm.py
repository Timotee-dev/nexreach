"""Talks to Claude.

Default backend runs Claude Code in headless mode (`claude -p`), so it uses the
Claude subscription you're logged into — no API key needed.
Set LLM_BACKEND=api in .env to use ANTHROPIC_API_KEY instead.
"""
import json
import re
import shutil
import subprocess
import tempfile

from django.conf import settings


class LLMError(Exception):
    pass


class LLMLimit(LLMError):
    """Claude usage limit reached; `resets_at` is when it comes back (or None if unknown)."""

    def __init__(self, message, resets_at=None):
        super().__init__(message)
        self.resets_at = resets_at


LIMIT_RE = re.compile(r"hit your (?:\w+ )?limit|usage limit|limit reached", re.I)
RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?", re.I)


def reset_time(text: str):
    """Turn 'resets 12am (Africa/Lagos)' into the next matching moment. Unknown -> 1 hour from now."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from django.utils import timezone

    now = timezone.now()
    m = RESET_RE.search(text or "")
    if not m:
        return now + timedelta(hours=1)
    hour, minute, ampm, tzname = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
    if ampm == "am" and hour == 12:
        hour = 0
    elif ampm == "pm" and hour != 12:
        hour += 12
    try:
        tz = ZoneInfo(tzname) if tzname else ZoneInfo(settings.TIME_ZONE)
    except Exception:
        tz = ZoneInfo(settings.TIME_ZONE)
    local = now.astimezone(tz)
    target = local.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target + timedelta(minutes=2)


def _fail(text: str):
    if LIMIT_RE.search(text or ""):
        raise LLMLimit(f"Claude usage limit reached: {text[:300]}", reset_time(text))
    raise LLMError(text)


def _claude_cli(prompt: str, model: str = "") -> str:
    binary = shutil.which(settings.CLAUDE_BIN) or settings.CLAUDE_BIN
    cmd = [binary, "-p", prompt, "--output-format", "json"]
    model = model or settings.LLM_MODEL
    if model:
        cmd += ["--model", model]
    # Run from an empty temp folder so Claude Code doesn't load your project files.
    with tempfile.TemporaryDirectory() as workdir:
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.LLM_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise LLMError(
                "Couldn't find the `claude` command. Install Claude Code and run `claude` once to log in, "
                "or set CLAUDE_BIN in .env to its full path."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError("Claude took too long to answer.") from exc

    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        _fail(f"Claude Code exited with {proc.returncode}: {(proc.stderr or '').strip()[:500]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out  # plain text output
    if isinstance(data, dict):
        if data.get("is_error"):
            _fail(f"Claude Code error: {str(data.get('result'))[:500]}")
        return str(data.get("result", ""))
    return out


def _api(prompt: str) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise LLMError("LLM_BACKEND=api needs `pip install anthropic` and ANTHROPIC_API_KEY in .env.") from exc
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=settings.LLM_MODEL or "claude-sonnet-5-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")


def ask(prompt: str, model: str = "") -> str:
    if settings.LLM_BACKEND == "api":
        return _api(prompt)
    return _claude_cli(prompt, model)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a reply (handles ```json fences and chatter)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"Expected JSON from Claude, got: {text[:300]}")
        candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Claude returned invalid JSON: {candidate[:300]}") from exc


def ask_json(prompt: str, model: str = "") -> dict:
    """model='' uses LLM_MODEL (emails). Pass settings.RESEARCH_MODEL for cheap sorting work."""
    prompt = (
        prompt.strip()
        + "\n\nReply with ONE JSON object only — no markdown, no explanation. Do not use any tools."
    )
    return extract_json(ask(prompt, model))


def cheap() -> str:
    return getattr(settings, "RESEARCH_MODEL", "") or ""

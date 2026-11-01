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


def _claude_cli(prompt: str) -> str:
    binary = shutil.which(settings.CLAUDE_BIN) or settings.CLAUDE_BIN
    cmd = [binary, "-p", prompt, "--output-format", "json"]
    if settings.LLM_MODEL:
        cmd += ["--model", settings.LLM_MODEL]
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
        raise LLMError(f"Claude Code exited with {proc.returncode}: {(proc.stderr or '').strip()[:500]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out  # plain text output
    if isinstance(data, dict):
        if data.get("is_error"):
            raise LLMError(f"Claude Code error: {str(data.get('result'))[:500]}")
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


def ask(prompt: str) -> str:
    if settings.LLM_BACKEND == "api":
        return _api(prompt)
    return _claude_cli(prompt)


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


def ask_json(prompt: str) -> dict:
    prompt = (
        prompt.strip()
        + "\n\nReply with ONE JSON object only — no markdown, no explanation. Do not use any tools."
    )
    return extract_json(ask(prompt))

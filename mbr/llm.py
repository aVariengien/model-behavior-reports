"""LLM calls with JSON-schema structured output: OpenRouter, or the Claude Code CLI (subscription)."""

import json
import os
import subprocess
import tempfile
import time

import httpx

from . import config, runlog

_client = httpx.Client(timeout=300)


def chat_json(model: str, content, schema: dict, name: str, max_tokens=2000, purpose="other",
              reasoning: str | None = None) -> dict:
    key = os.environ["OPENROUTER_API_KEY"]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": name, "strict": True, "schema": schema}},
        "max_tokens": max_tokens,
        "usage": {"include": True},
    }
    if reasoning:
        body["reasoning"] = {"effort": reasoning}
    error = None
    for attempt in range(4):
        try:
            r = _client.post(config.OPENROUTER_URL, json=body, headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": config.SITE_URL, "X-Title": "Model Behavior Reports"})
            if r.status_code == 200:
                data = r.json()
                if data.get("usage"):
                    runlog.llm_call(purpose, model, data["usage"])
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content")
                if text:
                    return json.loads(text[text.find("{"):text.rfind("}") + 1])
                error = str(data.get("error") or "empty response")
            else:
                error = f"HTTP {r.status_code}: {r.text[:300]}"
        except (httpx.TransportError, json.JSONDecodeError) as e:
            error = repr(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"OpenRouter call failed: {error}")


def claude_json(model: str, content: str, schema: dict, purpose="other") -> dict:
    """Headless `claude -p` on the logged-in Claude subscription: no per-token cost."""
    cmd = [config.CLAUDE_BIN, "-p", "--model", model, "--output-format", "json", "--json-schema", json.dumps(schema),
           "--tools", "", "--no-session-persistence"]
    error = None
    for attempt in range(3):
        try:
            # Empty cwd so no project CLAUDE.md leaks into the prompt.
            with tempfile.TemporaryDirectory() as cwd:
                p = subprocess.run(cmd, input=content, capture_output=True, text=True, cwd=cwd, timeout=900)
            if not p.stdout.strip():
                raise RuntimeError(f"exit {p.returncode}: {p.stderr[:300]}")
            data = json.loads(p.stdout)
            if not data.get("is_error") and data.get("structured_output"):
                u = data.get("usage") or {}
                runlog.llm_call(purpose, model, {
                    "prompt_tokens": sum(u.get(k) or 0 for k in
                                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                    "completion_tokens": u.get("output_tokens"), "cost": 0})
                return data["structured_output"]
            error = str(data.get("result") or data.get("subtype"))[:300]
        except (subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError) as e:
            error = repr(e)
        time.sleep(30 * (attempt + 1))
    raise RuntimeError(f"claude CLI call failed: {error}")

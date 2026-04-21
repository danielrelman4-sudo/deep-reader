"""LLM callable — supports both Anthropic SDK and Claude Code CLI."""

import os
import subprocess
import tempfile
import time
import sys

# Use SDK if ANTHROPIC_API_KEY is set, otherwise fall back to claude CLI
USE_SDK = bool(os.environ.get("ANTHROPIC_API_KEY"))

MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def claude_code_llm(prompt: str, retries: int = 3, max_tokens: int = 16000, model: str = "sonnet") -> str:
    """Send a prompt to Claude and return the response.

    Uses Anthropic SDK if ANTHROPIC_API_KEY is set, otherwise falls back
    to claude -p CLI.
    """
    if USE_SDK:
        return _sdk_call(prompt, retries, max_tokens, model)
    return _cli_call(prompt, retries, max_tokens, model)


def _sdk_call(prompt: str, retries: int, max_tokens: int, model: str) -> str:
    import anthropic
    model_id = MODEL_MAP.get(model, model)
    client = _get_client()

    for attempt in range(retries):
        try:
            text_parts = []
            with client.messages.stream(
                model=model_id,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    text_parts.append(text)
            return "".join(text_parts).strip()
        except anthropic.RateLimitError:
            print(f"  [retry {attempt+1}/{retries}] Rate limited, waiting 30s...", file=sys.stderr)
            time.sleep(30)
        except anthropic.APIStatusError as e:
            if "overloaded" in str(e).lower():
                print(f"  [retry {attempt+1}/{retries}] API overloaded, waiting 30s...", file=sys.stderr)
                time.sleep(30)
                continue
            raise
        except anthropic.APIConnectionError:
            print(f"  [retry {attempt+1}/{retries}] Connection error, waiting 10s...", file=sys.stderr)
            time.sleep(10)
    raise RuntimeError(f"Failed after {retries} retries")


def _cli_call(prompt: str, retries: int, max_tokens: int, model: str) -> str:
    for attempt in range(retries):
        try:
            cmd = ["claude", "-p", "--model", model]
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=tempfile.gettempdir(),
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "overloaded" in stderr.lower() or "rate" in stderr.lower():
                    print(f"  [retry {attempt+1}/{retries}] API issue, waiting 30s...", file=sys.stderr)
                    time.sleep(30)
                    continue
                raise RuntimeError(f"Claude Code exited with {result.returncode}: {stderr}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            print(f"  [retry {attempt+1}/{retries}] Timeout, retrying...", file=sys.stderr)
            time.sleep(10)
    raise RuntimeError(f"Failed after {retries} retries")

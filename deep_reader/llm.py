"""LLM callable that shells out to Claude Code CLI."""

import subprocess
import tempfile
import time
import sys


def claude_code_llm(prompt: str, retries: int = 3) -> str:
    """Send a prompt to Claude Code and return the response.

    Uses `claude -p` (print mode) which takes a prompt on stdin
    and returns the response on stdout, non-interactively.

    Runs from /tmp to avoid loading project context, which can
    confuse the model by mixing project files into the prompt.
    """
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", "sonnet"],
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
            continue

    raise RuntimeError(f"Failed after {retries} retries")

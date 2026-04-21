"""Inbox watcher — auto-ingests files dropped into vault/inbox/.

Uses mtime-based polling rather than a filesystem-event library so the watcher
can catch files that appear while it's down (on next startup, it processes
whatever's already in the inbox) and survives file-event quirks across
platforms. Polling interval is a few seconds; load is minimal.

Stability check: a file must have the same size + mtime for two consecutive
polls before it's ingested. This prevents picking up half-written files
while something is still copying into the inbox.
"""
from __future__ import annotations

import signal
import time
from pathlib import Path
from typing import Callable, Iterable

from rich.console import Console

from deep_reader.config import Config
from deep_reader.llm import claude_code_llm


SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf", ".docx", ".rtf"}
DEFAULT_INTERVAL = 5  # seconds between polls


console = Console()


def _scan(inbox: Path) -> dict[str, tuple[int, float]]:
    """Return {filename: (size, mtime)} for every candidate file in inbox."""
    out: dict[str, tuple[int, float]] = {}
    if not inbox.exists():
        return out
    for p in inbox.iterdir():
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        out[p.name] = (st.st_size, st.st_mtime)
    return out


def _ingest_one(config: Config, path: Path, llm: Callable) -> None:
    """Invoke the MCP-server-style ingest flow on a single file."""
    # Delegate to the MCP server helper to keep the code path identical.
    from deep_reader.mcp_server import (
        _auto_detect_type_path,
        _ingest_path,
        _raw_dir_for,
        _read_new_source,
    )

    stype = _auto_detect_type_path(path)
    console.print(f"[cyan]→[/cyan] {path.name} [dim]({stype})[/dim]")
    _ingest_path(config, path, stype)

    # After _ingest_path, path may have been converted (e.g. .pdf → .md).
    # Re-resolve the current name (keep same stem).
    final_candidates = list(path.parent.glob(f"{path.stem}.*"))
    current = final_candidates[0] if final_candidates else path

    dest_dir = _raw_dir_for(config, stype)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / current.name
    current.rename(final)
    _read_new_source(config, final, stype)
    console.print(f"[green]✓[/green] {path.name} → {final.relative_to(config.vault_root)}")


def watch(
    config: Config,
    interval: float = DEFAULT_INTERVAL,
    once: bool = False,
    llm: Callable = claude_code_llm,
) -> None:
    """Watch the inbox and auto-ingest new files.

    Set once=True to do one scan-and-ingest pass and return (useful for cron).
    Otherwise loops until interrupted with Ctrl-C or SIGTERM.
    """
    inbox = config.inbox
    inbox.mkdir(parents=True, exist_ok=True)

    if once:
        _process_stable_files(config, _scan(inbox), _scan(inbox), llm)
        return

    console.print(
        f"[bold]Watching[/bold] {inbox} "
        f"[dim](interval: {interval}s, Ctrl-C to stop)[/dim]\n"
    )

    stop = {"requested": False}

    def _handler(signum, frame):  # noqa: ARG001
        stop["requested"] = True

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    previous = _scan(inbox)
    # On first tick, files that are already sitting in the inbox and stable
    # (by definition, since they're not changing) should be processed.
    _process_stable_files(config, previous, previous, llm)

    while not stop["requested"]:
        time.sleep(interval)
        current = _scan(inbox)
        _process_stable_files(config, previous, current, llm)
        previous = current

    console.print("\n[dim]Watcher stopped.[/dim]")


def _process_stable_files(
    config: Config,
    previous: dict[str, tuple[int, float]],
    current: dict[str, tuple[int, float]],
    llm: Callable,
) -> None:
    """Ingest files whose size + mtime matched the previous snapshot."""
    for name, stat in current.items():
        if name not in previous:
            continue
        if previous[name] != stat:
            continue
        path = config.inbox / name
        if not path.exists():
            continue  # raced with another process
        try:
            _ingest_one(config, path, llm)
        except Exception as e:
            console.print(f"[red]✗[/red] {name}: {e}")

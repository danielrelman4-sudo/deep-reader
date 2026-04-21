"""Unified search across vault entities.

Shared by the CLI chat and the MCP server's `search` tool. Returns structured
results (lists of source/thread/concept/person/action matches) rather than a
synthesized answer — the caller decides how to present them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from deep_reader.config import Config
from deep_reader.state import GlobalState
from deep_reader.wiki import Wiki


@dataclass
class SearchResult:
    sources: list[dict] = field(default_factory=list)
    threads: list[dict] = field(default_factory=list)
    concepts: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)


def _tokenize(query: str) -> set[str]:
    q = query.lower()
    return {w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2}


def _score_text(tokens: set[str], text: str) -> int:
    if not text:
        return 0
    low = text.lower()
    return sum(1 for t in tokens if t in low)


def search(query: str, config: Config, limit: int = 10) -> SearchResult:
    state = GlobalState.load(config.state_file)
    wiki = Wiki(config)
    tokens = _tokenize(query)
    if not tokens:
        return SearchResult()

    result = SearchResult()

    # Sources (by overview text)
    scored_sources: list[tuple[int, str, str]] = []
    for slug in state.sources:
        overview = wiki.read_overview(slug) or ""
        score = _score_text(tokens, slug) * 3 + _score_text(tokens, overview)
        if score:
            snippet = _first_para(overview)
            scored_sources.append((score, slug, snippet))
    scored_sources.sort(reverse=True)
    for score, slug, snippet in scored_sources[:limit]:
        result.sources.append({"slug": slug, "score": score, "snippet": snippet})

    # Threads
    scored_threads: list[tuple[int, str, str]] = []
    for t in state.global_threads:
        content = wiki.read_thread(t) or ""
        score = _score_text(tokens, t) * 3 + _score_text(tokens, content)
        if score:
            thesis = _extract_section(content, "Thesis")
            scored_threads.append((score, t, thesis[:250]))
    scored_threads.sort(reverse=True)
    for score, name, thesis in scored_threads[:limit]:
        result.threads.append({"name": name, "score": score, "thesis": thesis})

    # Concepts
    scored_concepts: list[tuple[int, str, str]] = []
    for c in wiki.list_concepts():
        content = wiki.read_concept(c) or ""
        score = _score_text(tokens, c) * 3 + _score_text(tokens, content)
        if score:
            scored_concepts.append((score, c, _first_para(content)))
    scored_concepts.sort(reverse=True)
    for score, name, snippet in scored_concepts[:limit]:
        result.concepts.append({"name": name, "score": score, "snippet": snippet})

    # People
    scored_people: list[tuple[int, dict]] = []
    for p in state.people.values():
        corpus = " ".join([p.name, p.email or "", p.role or "", " ".join(p.aliases), p.summary])
        score = _score_text(tokens, corpus) * 2
        if score:
            scored_people.append((score, {
                "slug": p.slug, "name": p.name, "email": p.email,
                "role": p.role, "appearances": len(p.appearances),
            }))
    scored_people.sort(key=lambda x: x[0], reverse=True)
    for score, d in scored_people[:limit]:
        d["score"] = score
        result.people.append(d)

    # Action items (open only, both categories)
    scored_actions: list[tuple[int, dict]] = []
    for a in state.action_items:
        if a.status != "open":
            continue
        text = f"{a.description} {a.owner} {a.source}"
        score = _score_text(tokens, text)
        if score:
            scored_actions.append((score, {
                "id": a.id, "description": a.description, "owner": a.owner,
                "source": a.source, "category": a.category,
                "created_at": a.created_at.isoformat(),
            }))
    scored_actions.sort(key=lambda x: x[0], reverse=True)
    for score, d in scored_actions[:limit]:
        d["score"] = score
        result.action_items.append(d)

    return result


def _first_para(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith(("---", "#", "-", "*")):
            return s[:250]
    return ""


def _extract_section(content: str, heading: str) -> str:
    # Inline copy to avoid circular import
    lines = content.split("\n")
    in_section = False
    out = []
    for line in lines:
        if line.strip().startswith(f"## {heading}"):
            in_section = True
            continue
        if in_section and line.strip().startswith("## "):
            break
        if in_section:
            out.append(line)
    return "\n".join(out).strip()

"""Multi-item synthesis (Phase 2): the 'overall insight' layer.

Two products, both built on the same free-LLM chain as summarize.py:
  - throughline(items)         -> a 3-line "today's throughline" for the digest top
  - research_brief(query, ...) -> an on-demand briefing for research.py

Both degrade to a deterministic, no-LLM *heuristic* floor (not just a raw
snippet) so they still say something useful when no API key is present.
"""
from __future__ import annotations

import logging
from collections import Counter

from . import summarize

log = logging.getLogger("newsbot.synthesis")


def _item_lines(items: list[dict], limit: int) -> str:
    lines = []
    for it in items[:limit]:
        src = it.get("source", "?")
        title = (it.get("title") or "").strip()
        lines.append(f"- [{src}] {title}")
    return "\n".join(lines)


def _topic_spread(items: list[dict]) -> str:
    tags = Counter()
    for it in items:
        for t in it.get("tags") or []:
            tags[t] += 1
    if not tags:
        return ""
    top = ", ".join(f"{t} ({n})" for t, n in tags.most_common(4))
    return top


# --------------------------------------------------------------------------- #
# daily throughline (top of the digest)
# --------------------------------------------------------------------------- #
_THROUGHLINE_PROMPT = (
    "You are a sharp GTM/AI analyst writing the opening lines of a daily news "
    "digest. Below are today's {n} most relevant items. In AT MOST 3 short "
    "sentences, state the single throughline — what's actually happening across "
    "these today, the shift or tension worth noticing. No preamble, no bullet "
    "list, no restating headlines. If there's no real throughline, say so in one "
    "line.\n\nItems:\n{items}"
)


def _throughline_floor(items: list[dict]) -> str:
    """Deterministic fallback when no LLM is available."""
    n = len(items)
    spread = _topic_spread(items)
    srcs = Counter(it.get("source", "?") for it in items)
    lead = srcs.most_common(1)[0][0] if srcs else "various sources"
    base = f"{n} items today across {spread or 'several topics'}."
    if n:
        base += f" Most active source: {lead}. (Add a free GEMINI_API_KEY to turn this into a real synthesis.)"
    return base


def throughline(items: list[dict]) -> str:
    """A <=3-sentence 'what's happening today' line for the digest header."""
    if not items:
        return ""
    prompt = _THROUGHLINE_PROMPT.format(n=len(items), items=_item_lines(items, 40))
    out = summarize.complete(prompt, max_tokens=180)
    if out:
        log.info("throughline: LLM synthesis produced")
        return out
    log.info("throughline: no LLM available, using heuristic floor")
    return _throughline_floor(items)


# --------------------------------------------------------------------------- #
# on-demand research brief (research.py)
# --------------------------------------------------------------------------- #
_RESEARCH_PROMPT = (
    "You are a research analyst. A user asked: \"{query}\".\n\n"
    "Below are the {n} most recent relevant items pulled from across the web. "
    "Write a tight briefing: (1) a 2-3 sentence bottom-line on the current state "
    "of this topic, then (2) 3-5 bullet points of the concrete developments, each "
    "citing the source name in [brackets]. Be specific and skimmable. Only use "
    "what's in the items; if coverage is thin, say so.\n\nItems:\n{items}"
)


def _research_floor(query: str, items: list[dict]) -> str:
    """No-LLM fallback: a clean grouped list (still useful)."""
    lines = [f"Latest on: {query}", f"{len(items)} recent items found.\n"]
    for it in items:
        lines.append(f"• [{it.get('source','?')}] {(it.get('title') or '').strip()}")
    lines.append("\n(Add a free GEMINI_API_KEY for a synthesized briefing instead of a list.)")
    return "\n".join(lines)


def research_brief(query: str, items: list[dict]) -> str:
    """A synthesized briefing for an on-demand research query."""
    if not items:
        return f"No recent items found for: {query}"
    prompt = _RESEARCH_PROMPT.format(query=query, n=len(items), items=_item_lines(items, 25))
    out = summarize.complete(prompt, max_tokens=500)
    if out:
        log.info("research_brief: LLM synthesis produced")
        return out
    log.info("research_brief: no LLM available, using heuristic floor")
    return _research_floor(query, items)

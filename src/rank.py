"""Ranking: dedup, keyword relevance scoring, tier-aware noise gate, caps.

Returns items grouped by topic, each group sorted by score (desc).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher

from . import state

log = logging.getLogger("newsbot.rank")

TITLE_HIT_WEIGHT = 2
SUMMARY_HIT_WEIGHT = 1
FUZZY_TITLE_THRESHOLD = 0.85


def _count_hits(text: str, keywords: list[str]) -> int:
    text = (text or "").lower()
    return sum(1 for kw in keywords if kw.lower() in text)


def _recency_bonus(published: datetime | None) -> float:
    """Small monotonic bonus for newer items (0..2)."""
    if not published:
        return 0.0
    age_h = (datetime.now(timezone.utc) - published).total_seconds() / 3600.0
    if age_h <= 6:
        return 2.0
    if age_h <= 12:
        return 1.5
    if age_h <= 24:
        return 1.0
    return 0.5


def _dedup(items: list[dict]) -> list[dict]:
    """Drop exact-URL duplicates, then near-duplicate titles (same story, two sources)."""
    by_url: dict[str, dict] = {}
    for it in items:
        key = state.normalize_url(it["url"])
        # Prefer the higher-tier / earlier occurrence; first writer wins is fine here.
        if key not in by_url:
            by_url[key] = it
    unique = list(by_url.values())

    kept: list[dict] = []
    for it in unique:
        title = (it.get("title") or "").lower()
        dup = False
        for k in kept:
            if SequenceMatcher(None, title, (k.get("title") or "").lower()).ratio() > FUZZY_TITLE_THRESHOLD:
                dup = True
                break
        if not dup:
            kept.append(it)
    log.info("dedup: %d -> %d (url) -> %d (title)", len(items), len(unique), len(kept))
    return kept


def _score_and_assign(item: dict, topics: dict) -> tuple[float, str, int]:
    """Return (score, best_topic, total_keyword_hits) for an item.

    Score = weighted keyword hits (title weighted higher) + recency bonus.
    The item is assigned to the topic with the most hits (ties broken by the
    item's own declared tags, then first topic).
    """
    title = item.get("title", "")
    summary = item.get("summary", "")
    item_tags = set(item.get("tags") or [])

    best_topic = None
    best_topic_hits = -1
    total_hits = 0
    score = 0.0

    for topic, keywords in (topics or {}).items():
        t_hits = _count_hits(title, keywords)
        s_hits = _count_hits(summary, keywords)
        topic_hits = t_hits + s_hits
        total_hits += topic_hits
        score += TITLE_HIT_WEIGHT * t_hits + SUMMARY_HIT_WEIGHT * s_hits

        # Choose the best topic; nudge ties toward a topic the feed already tags.
        better = topic_hits > best_topic_hits
        tie_break = topic_hits == best_topic_hits and topic in item_tags and best_topic not in item_tags
        if better or tie_break:
            best_topic = topic
            best_topic_hits = topic_hits

    # If nothing matched a keyword, fall back to the feed's first declared tag.
    if best_topic is None or best_topic_hits <= 0:
        best_topic = (item.get("tags") or ["ai"])[0]

    score += _recency_bonus(item.get("published"))
    return score, best_topic, total_hits


def process(items: list[dict], topics: dict, cfg: dict) -> dict[str, list[dict]]:
    """Full ranking pipeline. Returns {topic: [items sorted by score desc]}."""
    noisy_tiers = set((cfg or {}).get("noisy_tiers") or [])
    max_per_topic = int((cfg or {}).get("max_per_topic", 6))
    max_total = int((cfg or {}).get("max_items_total", 30))

    deduped = _dedup(items)

    qualified: list[dict] = []
    for it in deduped:
        score, topic, hits = _score_and_assign(it, topics)
        tier = it.get("tier", "?")

        # Tier B (GitHub releases): a new release of a tracked tool is inherently
        # relevant — always include (subject to caps), keywords secondary.
        if tier == "B":
            it["_score"], it["_topic"] = score + 1.0, topic
            qualified.append(it)
            continue

        # Tier-aware noise gate: noisy tiers (Google Alerts / Reddit) need >=2 hits.
        min_hits = 2 if tier in noisy_tiers else 1
        if hits < min_hits:
            continue

        it["_score"], it["_topic"] = score, topic
        qualified.append(it)

    log.info("qualified: %d/%d items passed the noise gate", len(qualified), len(deduped))

    # Group by topic, sort each group, enforce per-topic cap.
    grouped: dict[str, list[dict]] = {}
    for it in sorted(qualified, key=lambda x: x["_score"], reverse=True):
        grouped.setdefault(it["_topic"], [])
        if len(grouped[it["_topic"]]) < max_per_topic:
            grouped[it["_topic"]].append(it)

    # Enforce the global cap while preserving topic grouping, taking the highest
    # scored items across all topics first.
    all_selected = sorted(
        (it for group in grouped.values() for it in group),
        key=lambda x: x["_score"],
        reverse=True,
    )[:max_total]
    selected_ids = {id(it) for it in all_selected}

    final: dict[str, list[dict]] = {}
    for topic, group in grouped.items():
        kept = [it for it in group if id(it) in selected_ids]
        if kept:
            final[topic] = kept

    total = sum(len(v) for v in final.values())
    log.info("ranked: %d items across %d topics (caps: %d/topic, %d total)",
             total, len(final), max_per_topic, max_total)
    return final

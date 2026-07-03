"""On-demand research: 'what's happening on topic X right now', for free.

    python -m src.research "AI agents in fintech"
    python -m src.research "6sense vs demandbase" --hours 336 --max 20
    python -m src.research "MCP protocol" --email        # also email the brief

Backend is Google News RSS + Hacker News search — no paid search API, consistent
with the project's free-forever rule. Produces a synthesized briefing when an
LLM key is present, else a clean ranked list.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

# Support both `python -m src.research` and `python src/research.py`.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src import ingest, state, synthesis, send  # type: ignore
else:
    from . import ingest, state, synthesis, send

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("newsbot.research")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _hn_search(query: str, min_points: int = 0) -> list[dict]:
    """Hacker News Algolia search for the query (no key)."""
    import requests

    items: list[dict] = []
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"tags": "story", "query": query},
            headers={"User-Agent": ingest.USER_AGENT},
            timeout=ingest.REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        for hit in r.json().get("hits", []):
            if (hit.get("points") or 0) < min_points:
                continue
            created = hit.get("created_at_i")
            items.append(
                {
                    "title": (hit.get("title") or "").strip(),
                    "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    "summary": f"{hit.get('points', 0)} points, {hit.get('num_comments', 0)} comments on HN",
                    "source": "Hacker News",
                    "tier": "A",
                    "published": datetime.fromtimestamp(created, tz=timezone.utc) if created else None,
                    "tags": [],
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("HN search failed (%s)", exc)
    return items


def gather(query: str, hours: int, max_items: int) -> list[dict]:
    """Fetch + dedup + recency-rank items for a query."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    raw: list[dict] = []
    # Primary backend: Google News RSS search for the exact query.
    raw.extend(ingest._fetch_feed({
        "url": ingest._google_news_url(query),
        "source": "Google News",
        "tier": "C",
        "tags": [],
    }))
    # Secondary: Hacker News stories matching the query.
    raw.extend(_hn_search(query))

    # Recency filter + URL dedup.
    seen: set[str] = set()
    fresh: list[dict] = []
    for it in raw:
        if not it.get("title") or not it.get("url"):
            continue
        pub = it.get("published")
        if pub is not None and pub < cutoff:
            continue
        key = state.normalize_url(it["url"])
        if key in seen:
            continue
        seen.add(key)
        fresh.append(it)

    # Sort newest first; undated items sink to the bottom.
    fresh.sort(key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    log.info("gathered %d items for '%s' (last %dh)", len(fresh), query, hours)
    return fresh[:max_items]


def _render_html(query: str, brief: str, items: list[dict]) -> str:
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    rows = []
    for it in items:
        rows.append(
            f'<li style="margin:6px 0;"><a href="{esc(it["url"])}">{esc(it["title"])}</a> '
            f'<span style="color:#868e96;font-size:12px;">— {esc(it.get("source",""))}</span></li>'
        )
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:680px;margin:0 auto;">'
        f'<h2>🔎 Research: {esc(query)}</h2>'
        f'<div style="background:#f3f0ff;border:1px solid #d0bfff;border-radius:8px;padding:14px 16px;'
        f'white-space:pre-wrap;font-size:14px;line-height:1.55;">{esc(brief)}</div>'
        f'<h3 style="margin-top:24px;">Sources ({len(items)})</h3><ul>{"".join(rows)}</ul></div>'
    )


def main() -> int:
    import yaml

    parser = argparse.ArgumentParser(description="On-demand topic research (free)")
    parser.add_argument("query", help="what to research, e.g. 'AI agents in fintech'")
    parser.add_argument("--hours", type=int, default=None, help="lookback window (default from config, 168)")
    parser.add_argument("--max", type=int, default=None, help="max items (default from config, 15)")
    parser.add_argument("--email", action="store_true", help="also email the brief")
    args = parser.parse_args()

    cfg = {}
    cfg_path = os.path.join(ROOT, "config.yaml")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    rcfg = cfg.get("research", {}) or {}
    hours = args.hours or int(rcfg.get("recency_hours", 168))
    max_items = args.max or int(rcfg.get("max_items", 15))

    items = gather(args.query, hours, max_items)
    brief = synthesis.research_brief(args.query, items)

    # Console output.
    print("\n" + "=" * 70)
    print(f"RESEARCH: {args.query}")
    print("=" * 70)
    print(brief)
    print("\nSOURCES:")
    for it in items:
        print(f"  • [{it.get('source','?')}] {it.get('title','')}\n    {it.get('url','')}")

    # HTML artifact.
    slug = re.sub(r"[^a-z0-9]+", "-", args.query.lower()).strip("-")[:50] or "research"
    out = os.path.join(ROOT, f"research_{slug}.html")
    html = _render_html(args.query, brief, items)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"\nWrote {out}")

    if args.email:
        send.send(f"🔎 Research: {args.query}", html, cfg["email_to"], cfg["email_from"])
        print(f"Emailed to {cfg['email_to']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

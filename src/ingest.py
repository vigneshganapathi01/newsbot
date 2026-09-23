"""Ingestion: pull items from every feed tier plus optional HN/Reddit boosters.

Everything here is fail-soft. A dead feed, a rate-limited endpoint, or a single
malformed entry logs a WARN and is skipped — the run continues. Returns a flat
list of dict items:

    {title, url, summary, source, tier, published (datetime, UTC), tags}
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, parse_qs, quote

import feedparser
import requests

from . import state

log = logging.getLogger("newsbot.ingest")

USER_AGENT = "newsbot/1.0 (+https://github.com/) daily-digest"
REQUEST_TIMEOUT = 20
PLACEHOLDER = "PASTE_ALERT_RSS_URL"

_now = lambda: datetime.now(timezone.utc)  # noqa: E731


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _entry_datetime(entry) -> datetime | None:
    """Best-effort published/updated time as timezone-aware UTC."""
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime.fromtimestamp(calendar.timegm(tm), tz=timezone.utc)
            except Exception:  # noqa: BLE001
                continue
    return None


def _clean_summary(entry) -> str:
    raw = entry.get("summary") or entry.get("description") or ""
    # feedparser hands back HTML in summaries; strip tags crudely for the snippet.
    import re

    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _google_news_url(query: str) -> str:
    """Build a free Google News RSS search feed URL for any query.

    Google News aggregates the whole web for a query with no API key — this is
    what gives 'overall internet' coverage. Article links are news.google.com
    redirect wrappers that resolve to the real story on click; they're stable
    per article so dedup still works.
    """
    return (
        "https://news.google.com/rss/search?q="
        f"{quote(query)}&hl=en-US&gl=US&ceid=US:en"
    )


def _unwrap_google_alert(url: str) -> str:
    """Google Alerts entry links are redirect wrappers:
    https://www.google.com/url?...&url=<REAL>&... — pull out the real target.
    """
    try:
        parts = urlsplit(url)
        if "google.com" in (parts.hostname or "") and parts.path.startswith("/url"):
            qs = parse_qs(parts.query)
            for key in ("url", "q"):
                if key in qs and qs[key]:
                    return qs[key][0]
    except Exception:  # noqa: BLE001
        pass
    return url


# --------------------------------------------------------------------------- #
# feed ingestion (Tier A / B / C — all parse identically)
# --------------------------------------------------------------------------- #
def _fetch_feed(feed: dict) -> list[dict]:
    url = feed.get("url", "")
    source = feed.get("source", "?")
    tier = feed.get("tier", "?")
    tags = feed.get("tags", []) or []

    if not url or url == PLACEHOLDER:
        log.info("skip unset feed %s (%s)", source, tier)
        return []

    items: list[dict] = []
    try:
        # Fetch via requests (proper CA bundle + enforced timeout), then parse the
        # bytes. This avoids feedparser's built-in urllib fetch, which has no
        # timeout and often no CA certs — a hanging or TLS-broken feed would
        # otherwise stall or silently fail the run.
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            log.warning("WARN dead feed %s %s (%s)", source, url, parsed.get("bozo_exception"))
            return []
        is_google_alert = "google.com/alerts" in url or "alerts" in url and "google" in url
        for entry in parsed.entries:
            try:
                link = entry.get("link") or ""
                if tier == "C" or is_google_alert:
                    link = _unwrap_google_alert(link)
                if not link:
                    continue
                items.append(
                    {
                        "title": (entry.get("title") or "").strip(),
                        "url": link,
                        "summary": _clean_summary(entry),
                        "source": source,
                        "tier": tier,
                        "published": _entry_datetime(entry),
                        "tags": list(tags),
                    }
                )
            except Exception as exc:  # noqa: BLE001 — one bad entry never kills the feed
                log.warning("WARN bad entry in %s (%s)", source, exc)
                continue
        log.info("ok feed %s (%s): %d entries", source, tier, len(items))
    except Exception as exc:  # noqa: BLE001
        log.warning("WARN dead feed %s %s (%s)", source, url, exc)
    return items


# --------------------------------------------------------------------------- #
# optional boosters (no API keys)
# --------------------------------------------------------------------------- #
def _fetch_hn(cfg: dict) -> list[dict]:
    hn = (cfg or {}).get("hn") or {}
    if not hn.get("enabled"):
        return []
    queries = hn.get("queries") or []
    min_points = int(hn.get("min_points", 30))
    items: list[dict] = []
    for q in queries:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"tags": "story", "query": q},
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            for hit in r.json().get("hits", []):
                if (hit.get("points") or 0) < min_points:
                    continue
                link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                created = hit.get("created_at_i")
                published = (
                    datetime.fromtimestamp(created, tz=timezone.utc) if created else None
                )
                items.append(
                    {
                        "title": (hit.get("title") or "").strip(),
                        "url": link,
                        "summary": f"{hit.get('points', 0)} points on Hacker News",
                        "source": "Hacker News",
                        "tier": "A",  # HN is high-signal once point-gated
                        "published": published,
                        "tags": ["ai", "agents", "gtm"],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("WARN HN query failed '%s' (%s)", q, exc)
    log.info("ok HN booster: %d items", len(items))
    return items


def _fetch_google_news(cfg: dict) -> list[dict]:
    """Broad whole-web coverage: one Google News RSS search per configured topic."""
    gn = (cfg or {}).get("google_news") or {}
    if not gn.get("enabled"):
        return []
    queries = gn.get("queries") or []
    items: list[dict] = []
    for entry in queries:
        if isinstance(entry, dict):
            q = entry.get("q", "")
            tags = entry.get("tags", []) or []
            src = entry.get("source", "Google News")  # optional per-query label
        else:
            q, tags, src = str(entry), [], "Google News"
        if not q:
            continue
        feed = {
            "url": _google_news_url(q),
            "source": src,
            "tier": "C",  # noisy → subject to the >=2 keyword-hit gate
            "tags": tags,
        }
        items.extend(_fetch_feed(feed))
    log.info("ok Google News: %d items across %d queries", len(items), len(queries))
    return items


def _fetch_reddit(cfg: dict) -> list[dict]:
    rd = (cfg or {}).get("reddit") or {}
    if not rd.get("enabled"):
        return []
    subs = rd.get("subreddits") or []
    limit = int(rd.get("limit", 25))
    items: list[dict] = []
    for sub in subs:
        try:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/new.json",
                params={"limit": limit},
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            for child in r.json().get("data", {}).get("children", []):
                d = child.get("data", {})
                permalink = d.get("permalink")
                link = f"https://www.reddit.com{permalink}" if permalink else d.get("url")
                if not link:
                    continue
                created = d.get("created_utc")
                published = (
                    datetime.fromtimestamp(created, tz=timezone.utc) if created else None
                )
                items.append(
                    {
                        "title": (d.get("title") or "").strip(),
                        "url": link,
                        "summary": (d.get("selftext") or "")[:300],
                        "source": f"r/{sub}",
                        "tier": "C",  # Reddit is noisy — treat like Tier C
                        "published": published,
                        "tags": ["gtm", "sales", "marketing"],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("WARN Reddit sub failed '%s' (%s)", sub, exc)
    log.info("ok Reddit booster: %d items", len(items))
    return items


# --------------------------------------------------------------------------- #
# public entrypoint
# --------------------------------------------------------------------------- #
def fetch_all(feeds: list[dict], cfg: dict, seen: set[str]) -> list[dict]:
    """Fetch every feed + boosters, then drop stale and already-seen items."""
    recency_hours = int((cfg or {}).get("recency_hours", 26))
    cutoff = _now() - timedelta(hours=recency_hours)

    gn_cfg = (cfg or {}).get("google_news") or {}
    activate_c = bool(gn_cfg.get("activate_tier_c"))

    raw: list[dict] = []
    for feed in feeds or []:
        f = feed
        # Auto-activate unconfigured Tier-C companies via Google News instead of
        # requiring a manual Google Alert per company. Uses the entry's `query`
        # field if present, else the quoted source name.
        if activate_c and feed.get("tier") == "C" and feed.get("url") == PLACEHOLDER:
            query = feed.get("query") or f'"{feed.get("source", "")}"'
            f = dict(feed, url=_google_news_url(query))
        raw.extend(_fetch_feed(f))
    raw.extend(_fetch_google_news(cfg))
    raw.extend(_fetch_hn(cfg))
    raw.extend(_fetch_reddit(cfg))

    fresh: list[dict] = []
    dropped_old = dropped_seen = dropped_bad = 0
    for item in raw:
        if not item.get("url") or not item.get("title"):
            dropped_bad += 1
            continue
        published = item.get("published")
        # If a feed omits a date, keep the item (better a false-fresh than a silent miss);
        # dedup via seen.json still prevents re-sends across days.
        if published is not None and published < cutoff:
            dropped_old += 1
            continue
        if state.normalize_url(item["url"]) in seen:
            dropped_seen += 1
            continue
        fresh.append(item)

    log.info(
        "ingest: %d raw -> %d fresh (dropped %d old, %d seen, %d bad)",
        len(raw), len(fresh), dropped_old, dropped_seen, dropped_bad,
    )
    return fresh

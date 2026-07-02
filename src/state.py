"""Persistent dedup state: which URLs have already been sent.

seen.json is a flat JSON list of normalized URLs. It is committed back by the
GitHub Actions workflow after every run so re-sends never happen. Everything here
is fail-soft: a missing or corrupt file yields an empty set, never a crash.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

log = logging.getLogger("newsbot.state")

# Keep the file bounded so it doesn't grow forever.
MAX_SEEN = 2500

# Query params that are pure tracking noise — strip them so the same article
# arriving with different UTM tags dedups correctly.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "ref_src", "source",
    "__hstc", "__hssc", "__hsfp", "_hsenc", "_hsmi", "igshid", "spm",
}


def normalize_url(url: str) -> str:
    """Canonicalize a URL for dedup: lowercase host, drop tracking params,
    drop fragment, strip a trailing slash on the path.

    Returns the input unchanged if it can't be parsed (never raises).
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower() or "https"
        host = parts.hostname.lower() if parts.hostname else ""
        # Preserve a non-default port if present.
        if parts.port:
            netloc = f"{host}:{parts.port}"
        else:
            netloc = host
        # Filter query params.
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=False)
            if k.lower() not in _TRACKING_KEYS
            and not k.lower().startswith(_TRACKING_PREFIXES)
        ]
        query = urlencode(kept)
        path = parts.path.rstrip("/") if parts.path != "/" else "/"
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:  # noqa: BLE001 — normalization must never crash the run
        return url.strip()


def load_seen(path: str = "seen.json") -> set[str]:
    """Read seen.json → set of normalized URLs. Missing/corrupt → empty set."""
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            log.warning("seen.json is not a list; treating as empty")
            return set()
        return {normalize_url(u) for u in data if isinstance(u, str) and u}
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s (%s); starting with empty seen set", path, exc)
        return set()


def save_seen(urls: set[str], path: str = "seen.json") -> None:
    """Write the seen set back, pruned to the most recent ~MAX_SEEN URLs.

    Sets are unordered so 'most recent' is best-effort; the cap is a size guard,
    not a strict recency window. Writing is atomic-ish via a temp file + replace.
    """
    normalized = sorted({normalize_url(u) for u in urls if u})
    if len(normalized) > MAX_SEEN:
        normalized = normalized[-MAX_SEEN:]
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=0, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not write %s (%s)", path, exc)

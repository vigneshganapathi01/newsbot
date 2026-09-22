"""Orchestration entrypoint.

    python -m src.main            # full run (fetch -> rank -> [summarize] -> send)
    python -m src.main --local    # dry run: fetch -> rank -> write digest.html, no email

The whole run is wrapped in a top-level try/except: on unexpected failure we
still attempt to email the error text, so a broken run is visible, not silent.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback

import yaml

# Support both `python -m src.main` (package) and `python src/main.py` (script).
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src import digest, ingest, rank, send, state  # type: ignore
    from src import summarize, synthesis  # type: ignore
else:
    from . import digest, ingest, rank, send, state
    from . import summarize, synthesis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("newsbot.main")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_yaml(name: str) -> dict:
    path = os.path.join(ROOT, name)
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _flatten(grouped: dict[str, list[dict]]) -> list[dict]:
    return [it for group in grouped.values() for it in group]


def resolve_emails(cfg: dict) -> tuple[str, str]:
    """Resolve (to, from) addresses without committing a personal email.

    Precedence: env EMAIL_TO/EMAIL_FROM → config value → GMAIL_USER (the address
    you already authenticate with). Keeps the public repo free of personal data.
    """
    gmail_user = os.environ.get("GMAIL_USER", "")
    to = os.environ.get("EMAIL_TO") or cfg.get("email_to") or gmail_user
    frm = os.environ.get("EMAIL_FROM") or cfg.get("email_from") or gmail_user
    return to, frm


def run(local: bool = False) -> int:
    feeds = _load_yaml("feeds.yaml").get("feeds", [])
    topics = _load_yaml("topics.yaml").get("topics", {})
    cfg = _load_yaml("config.yaml")
    seen_path = os.path.join(ROOT, "seen.json")

    seen = state.load_seen(seen_path)
    log.info("loaded %d seen URLs", len(seen))

    items = ingest.fetch_all(feeds, cfg, seen)
    grouped = rank.process(items, topics, cfg)

    throughline = ""
    if cfg.get("use_llm"):
        log.info("use_llm=true → summarizing %d items", sum(len(v) for v in grouped.values()))
        for group in grouped.values():
            for it in group:
                it["summary"] = summarize.summarize(it)

    # The daily throughline synthesizes the day's items into a "what's happening"
    # line. Enabled independently of per-item summaries via config.
    if cfg.get("throughline", {}).get("enabled") and cfg.get("use_llm"):
        flat = _flatten(grouped)
        throughline = synthesis.throughline(flat)

    subject, body, is_empty = digest.build(grouped, cfg, throughline=throughline)

    if local:
        out = os.path.join(ROOT, "digest.html")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(body)
        log.info("[local] wrote %s (subject: %s, empty=%s)", out, subject, is_empty)
        return 0

    to_addr, from_addr = resolve_emails(cfg)
    send.send(subject, body, to_addr, from_addr)

    # Persist what we sent so it never re-sends. (Empty digests add nothing.)
    sent = _flatten(grouped)
    if sent:
        state.save_seen(seen | {it["url"] for it in sent}, seen_path)
        log.info("saved seen.json (+%d new)", len(sent))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NewsBot daily digest")
    parser.add_argument("--local", action="store_true",
                        help="dry run: write digest.html instead of emailing")
    args = parser.parse_args()

    try:
        return run(local=args.local)
    except Exception:  # noqa: BLE001 — surface any failure loudly
        err = traceback.format_exc()
        log.error("run failed:\n%s", err)
        if not args.local:
            # Best-effort: email the error to ourselves so a broken run is visible.
            try:
                cfg = _load_yaml("config.yaml")
                to_addr, from_addr = resolve_emails(cfg)
                send.send(
                    "⚠️ NewsBot run FAILED",
                    f"<h3>NewsBot crashed</h3><pre>{err}</pre>",
                    to_addr,
                    from_addr,
                )
            except Exception as exc2:  # noqa: BLE001
                log.error("could not send failure email: %s", exc2)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

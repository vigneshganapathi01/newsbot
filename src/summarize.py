"""Phase 2 — optional free-LLM summarization. Only runs when config use_llm: true.

Fallback chain: Gemini (free) -> Groq -> OpenRouter free model -> no-LLM floor.
Every provider is skipped silently when its API key is absent. The no-LLM floor
guarantees the system keeps working even if every free tier changes its terms.

Phase 2 is intentionally inert until Phase 1 has emailed a correct, dedup'd
digest for 2 consecutive days (see the build spec). Keep use_llm: false until then.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("newsbot.summarize")

_PROMPT = (
    "Summarize this news item in at most 2 concise sentences for a busy "
    "GTM/AI professional. No preamble.\n\nTitle: {title}\n\nContent: {content}"
)
_TIMEOUT = 25


def _floor(item: dict) -> str:
    """No-LLM fallback: the raw snippet, capped."""
    return (item.get("summary") or "")[:200]


def _gemini(prompt: str, max_tokens: int) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-1.5-flash-latest:generateContent?key={key}"
        )
        r = requests.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("gemini failed (%s)", exc)
        return None


def _groq(prompt: str, max_tokens: int) -> str | None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("groq failed (%s)", exc)
        return None


def _openrouter(prompt: str, max_tokens: int) -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "meta-llama/llama-3.1-8b-instruct:free",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("openrouter failed (%s)", exc)
        return None


def complete(prompt: str, max_tokens: int = 120) -> str | None:
    """Run a prompt through the free-LLM fallback chain.

    Gemini -> Groq -> OpenRouter. Returns the first non-empty completion, or
    None if every provider is absent/failed (so callers can apply their own
    no-LLM floor). This is the shared primitive for both per-item summaries
    (summarize) and multi-item synthesis (synthesis.py).
    """
    for provider in (_gemini, _groq, _openrouter):
        out = provider(prompt, max_tokens)
        if out:
            return out
    return None


def summarize(item: dict) -> str:
    """Return a <=2-sentence summary, degrading gracefully to the raw snippet."""
    content = (item.get("summary") or "")[:1500]
    prompt = _PROMPT.format(title=item.get("title", ""), content=content)
    return complete(prompt, max_tokens=120) or _floor(item)

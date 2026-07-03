# NewsBot — Free Daily AI/GTM News Digest to Email

A cron-triggered pipeline that collects AI / automation / agents / marketing / sales /
GTM / ABM / TAM news each day from a fixed list of authoritative sources and emails
it to you — with source links — using **zero paid subscriptions and no paid APIs**.

- **Free forever.** No paid tiers as load-bearing dependencies.
- **Fail-soft.** A dead feed, rate-limited endpoint, or bad entry logs and skips — never crashes the run.
- **Config, not code.** All sources and keywords live in YAML.
- **Phase 1 needs zero API keys.** LLM summarization is additive, off by default.
- **No duplicates.** `seen.json` (committed back by the workflow) prevents re-sends.
- **Feeds, not scrapers.** Every source is an Atom/RSS feed — including GitHub releases and Google Alerts.

---

## Quick start (local test — no email needed)

```bash
cd newsbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Dry run: fetches + ranks, writes digest.html, sends NOTHING.
python -m src.main --local
open digest.html            # inspect the result in a browser
```

## Send a real email locally

```bash
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"   # 16-char Google App Password
python -m src.main
```

---

## How it works

```
main.py
  1. seen   = state.load_seen()                     # seen.json → set of URLs
  2. items  = ingest.fetch_all(feeds, config, seen) # all tiers + optional HN/Reddit
  3. ranked = rank.process(items, topics, config)   # dedup + tier-aware noise gate
  4. if use_llm: summarize.summarize() each item    # Phase 2, optional
  5. html   = digest.build(ranked, config)          # grouped HTML, linked titles
  6. send.send(...)                                  # Gmail SMTP (SSL 465)
  7. state.save_seen(seen | sent_urls)               # persist, pruned to ~2500
```

### Source tiers (all reached via feeds — no scraping)

| Tier | Mechanism | Reliability | Examples |
|------|-----------|-------------|----------|
| **A** | Native RSS/Atom | High | OpenAI, Anthropic, HuggingFace, LangChain, Zapier, HubSpot, Ahrefs, Semrush |
| **B** | `github.com/<org>/<repo>/releases.atom` | High | CrewAI, LangGraph, AutoGen, LlamaIndex, Flowise, OpenAI Agents SDK, MCP |
| **C** | Google Alerts RSS | Medium (noisy) | Clay, Apollo, Outreach, ZoomInfo, 6sense, Demandbase, Gartner, Forrester … |

Tier C entries ship with a `PASTE_ALERT_RSS_URL` placeholder — unset ones just log and skip,
so the pipeline runs fine before you've filled them all in.

---

## Configuration (edit YAML, never Python)

- **`feeds.yaml`** — every source, tagged by topic + tier.
- **`topics.yaml`** — keywords per topic (case-insensitive substring match on title + summary).
- **`config.yaml`** — recency window, caps, `use_llm` flag, email addresses, timezone, noisy tiers, HN/Reddit boosters.

### Relevance & noise gate
- Score = weighted keyword hits (title > summary) + recency bonus; item assigned to its best-matching topic.
- **Tier A/B** need ≥1 keyword hit. **Tier C (noisy)** needs ≥2.
- **Tier B (GitHub releases)** are always included up to caps — a new release of a tracked tool is inherently relevant.
- Caps: `max_per_topic` and `max_items_total` (precision over breadth).

---

## Google Alerts setup (Tier C) — one-time

For each Tier-C source:
1. Go to **alerts.google.com**.
2. Enter the query (suggested queries are in `feeds.yaml` comments).
3. Click **Show options** → **Deliver to: RSS feed**.
4. Create the alert, then copy the generated **RSS feed URL**.
5. Paste it into `feeds.yaml`, replacing that source's `PASTE_ALERT_RSS_URL`.

Do a batch of ~5/day if tedious. Unset entries are skipped, not fatal.

---

## GitHub Actions deployment (one-time)

1. Push this repo to GitHub.
2. **Gmail App Password:** enable 2FA → Google Account → Security → App Passwords → generate → copy 16 chars.
3. Repo **Settings → Secrets and variables → Actions** → add `GMAIL_USER`, `GMAIL_APP_PASSWORD`. (LLM keys later.)
4. Commit an empty `seen.json` containing `[]` (already present).
5. **Actions → daily-newsbot → Run workflow** to test before trusting the cron.
6. Tune `feeds.yaml` / `topics.yaml` over the first week based on what lands.

The workflow runs at **01:30 UTC (07:00 IST)** daily and commits `seen.json` back after each run.

---

## Phase 2 — insight layer (free-LLM)

**Do not enable until Phase 1 has emailed a correct, dedup'd digest for 2 consecutive days.**

Set `use_llm: true` in `config.yaml` and add any of these GitHub secrets:
`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` (Gemini free tier is the easiest).

Fallback chain: **Gemini → Groq → OpenRouter → floor**. Absent keys are skipped silently,
so nothing ever breaks — it just degrades:
- **Per-item summaries** → floor is the raw snippet.
- **Daily throughline / research brief** → floor is a deterministic heuristic (still useful).

### Daily throughline — "overall insight," not more volume
A ≤3-sentence synthesis of the day's items at the **top of the digest** — the *what's actually
happening / what shifted* line that vendor blogs can't give you. Controlled by:
```yaml
throughline: { enabled: true }   # needs use_llm: true + an LLM key to synthesize
```
Without a key it shows a one-line heuristic (item/topic/source counts) so you can see the slot.

### On-demand research — "search everything on X right now" (free)
A separate tool for ad-hoc deep-dives, backed by **Google News RSS + Hacker News** (no paid
search API):
```bash
python -m src.research "AI agents in fintech"
python -m src.research "6sense vs demandbase" --hours 336 --max 20
python -m src.research "model context protocol" --email    # also email the brief
```
Fetches the most recent relevant items across the web, dedups, and — with an LLM key —
returns a synthesized briefing (bottom-line + cited bullets). Without a key: a clean ranked
list. Writes `research_<topic>.html` and prints to the console.

> **Design note:** "pull from *all* websites" is deliberately **not** a goal — more input ≠ more
> insight. Coverage is a curated set of high-signal feeds + broad Google News topic searches;
> the *insight* comes from the synthesis layer above, not from scraping everything.

---

## Honest limits

- **Gartner/Forrester:** research is paywalled — free coverage is press-release level only (via Google Alerts).
- **Google Alerts feeds are noisier** than native blogs; the ≥2-hit gate mitigates but doesn't eliminate it.
- **LinkedIn/X:** excluded by design — not reachable free without fragile scraping.

## Cost
**₹0.** Free public feeds + GitHub releases + Google Alerts RSS + Gmail SMTP + GitHub Actions only.

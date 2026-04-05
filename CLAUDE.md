# CLAUDE.md — Playbook Project Context

This file is loaded at the start of every Claude Code session.
Always read this before writing any code or making suggestions.

---

## What This Project Is

**Playbook** is a baseball betting intelligence platform (brand: playbook.edge).
The goal is to automate the process of finding edges in MLB betting markets by:
1. Scraping live odds and Statcast/FanGraphs pitcher/batter data
2. Running statistical models to identify mispriced lines
3. Sending alerts to Discord when a high-confidence edge is found

The owner has no coding experience. Keep all explanations simple and jargon-free.

---

## GitHub

- Repo: https://github.com/playbook-edge/playbook
- Account: playbook-edge
- Default branch: master

---

## Project Structure

```
playbook/
├── data/
│   ├── raw/          # Raw data exactly as pulled — never modify these
│   ├── processed/    # Cleaned, analysis-ready versions of raw data
│   └── historical/   # Past game results for backtesting
├── models/           # Edge calculators and prediction logic (not built yet)
├── scrapers/         # Data-pulling scripts
├── alerts/           # Discord notification logic (not built yet)
├── logs/             # Runtime logs (not built yet)
├── research/         # Notes, experiments, one-off analysis
├── config.py         # All settings loaded from .env
├── main.py           # Entry point — will eventually run the full bot
└── requirements.txt  # All dependencies
```

---

## What Has Been Built

### `scrapers/baseball_savant.py` — WORKING
Pulls today's MLB probable starters and their last-30-day Statcast stats.

**How it works:**
- Fetches today's schedule from the free MLB Stats API (no key needed)
- For each pitcher, calls `pybaseball.statcast_pitcher()` to get pitch-by-pitch data
- Computes K%, fastball velocity, and BABIP from raw Statcast rows
- Tries to fetch xFIP from FanGraphs via `pybaseball.pitching_stats_range()` — this fails early in the season when FanGraphs has no qualifying table yet, so xFIP shows as None until ~mid-April
- Saves output to `data/raw/savant_today.csv`
- Uses a 5-second delay between requests to avoid rate limiting
- pybaseball cache is enabled — re-runs within the same day are instant

**Run it:**
```
python scrapers/baseball_savant.py
```

**Output columns:** `name, team, k_pct, xfip, velo, babip`

**Known limitations:**
- xFIP is None early in the season (FanGraphs table is empty until enough PA accumulate)
- Pitchers with zero appearances in the 30-day window show NaN for all stats
- Some accented/special characters in pitcher names print as garbled on Windows terminal — the data in the CSV is correct

---

## Dependencies (all installed)

| Package | Purpose |
|---|---|
| `pybaseball` | Statcast + FanGraphs data |
| `anthropic` | Claude AI for analysis |
| `requests` | HTTP calls to MLB Stats API and other sources |
| `supabase` | Database (not connected yet) |
| `apscheduler` | Scheduling the bot to run automatically (not set up yet) |
| `python-dotenv` | Loads `.env` into environment |
| `discord-webhook` | Sends alerts to Discord (not set up yet) |
| `pandas` | Data wrangling |
| `numpy` | Math |

---

## Environment Variables

See `.env.example` for the full list. Keys needed:
- `ANTHROPIC_API_KEY` — for AI analysis
- `SUPABASE_URL` + `SUPABASE_KEY` — for the database
- `DISCORD_WEBHOOK_URL` — for bet alerts
- `ODDS_API_KEY` — for live odds (the-odds-api.com)

Real values go in `.env` (gitignored — never committed).

---

## What to Build Next (rough order)

1. **Odds scraper** — pull today's MLB moneylines and totals from the Odds API
2. **Edge calculator** — compare model-implied probability to the market line
3. **Claude analysis** — prompt Claude to rate each game given pitcher stats + odds
4. **Discord alerts** — send formatted alert when edge exceeds threshold
5. **Scheduler** — run the full pipeline automatically each morning
6. **Supabase logging** — store results and track bet outcomes over time

---

## Coding Rules for This Project

- No coding experience on the owner's side — always explain what you built and why in plain English after writing code
- Test every script immediately after writing it and fix errors before handing back
- Never commit `.env` — it contains real API keys
- Keep scrapers gentle on rate limits (5-second delays minimum)
- Save all raw data to `data/raw/` before any processing
- Prefer simple, readable code over clever one-liners

# CLAUDE.md — Playbook Project Context

This file is loaded at the start of every Claude Code session.
Always read this before writing any code or making suggestions.

---

## What This Project Is

**Playbook** is a baseball betting intelligence platform (brand: playbook.edge).
The goal is to automate the process of finding edges in MLB betting markets by:
1. Scraping live odds and Statcast/FanGraphs pitcher/batter data
2. Comparing current performance against 2-year historical baselines
3. Running a Poisson-based model to identify mispriced strikeout props
4. Sending tier-badged alerts to Discord when a high-confidence edge is found

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
│   ├── raw/              # Fresh data pulled each run — never modify manually
│   ├── processed/        # Model outputs (ev_signals.csv)
│   └── historical/       # 2024-2025 season stats + player baselines
├── models/               # EV calculator and player baseline builder
├── scrapers/             # Data-pulling scripts
├── alerts/               # Discord notification logic
├── logs/                 # Daily pipeline logs (pipeline_YYYY-MM-DD.log)
├── research/             # Notes, experiments, one-off analysis
├── config.py             # All settings loaded from .env
├── main.py               # Full pipeline — runs all 6 steps in order
├── run_playbook.bat      # Double-click or schedule via Task Scheduler
├── requirements.txt      # All dependencies
├── .env.example          # Template — copy to .env and fill in keys
└── .gitignore            # Keeps .env and data files off GitHub
```

---

## Full Pipeline (main.py — 6 steps)

Run manually: `python main.py`
Scheduled via: Windows Task Scheduler pointing at `run_playbook.bat` (set to 10:30am daily)

```
Step 1  scrapers/baseball_savant.py   →  data/raw/savant_today.csv
Step 2  scrapers/fangraphs.py         →  data/raw/pitcher_stats.csv
                                          data/raw/team_krates.csv
Step 3  scrapers/historical_stats.py  →  data/historical/pitcher_stats_2024.csv
                                          data/historical/pitcher_stats_2025.csv
                                          data/historical/pitcher_stats_all.csv
Step 4  models/player_baseline.py     →  data/historical/player_baselines.csv
Step 5  scrapers/odds_api.py          →  data/raw/todays_props.csv
Step 6  models/ev_calculator.py       →  data/processed/ev_signals.csv
                                          → fires Discord alerts automatically
```

Each step is isolated — one failure doesn't stop the rest.
A pipeline summary embed is also sent to Discord after all steps complete.

---

## What Has Been Built

### `scrapers/baseball_savant.py` — WORKING
- Fetches today's probable starters from the free MLB Stats API (no key needed)
- For each pitcher: calls `pybaseball.statcast_pitcher()` for last-30-day pitch data
- Computes K%, fastball velocity, BABIP from raw Statcast rows
- Tries FanGraphs xFIP via `pitching_stats_range()` — fails early in season (empty table), xFIP shows None until ~mid-April
- 5-second delay between requests; pybaseball cache enabled
- **Known quirk**: accented names (e.g. Vásquez) print garbled in Windows terminal — data in CSV is correct

### `scrapers/fangraphs.py` — WORKING
- Team strikeout rates by batter handedness (vs RHP / vs LHP) from Statcast
- Current season pitching leaderboard: xFIP, FIP, K/9, BB/9, BABIP
- Batting team derived from `home_team`/`away_team` + `inning_topbot` (no direct column)

### `scrapers/historical_stats.py` — WORKING
- Pulls full 2024 and 2025 FanGraphs season stats via `pitching_stats(year, year, qual=0)`
- Skips re-fetch if file already exists (cached to disk)
- Saves per-year CSVs + combined `pitcher_stats_all.csv`
- Run once to seed; re-run at end of each season to add the new year

### `scrapers/odds_api.py` — WORKING (needs API key)
- Fetches today's MLB events, then per-event pitcher strikeout props
- Books: DraftKings, FanDuel, BetMGM
- Requires `ODDS_API_KEY` in `.env` (the-odds-api.com — free tier: 500 req/month)
- Props post around 9-10am ET on game days; empty file before then is normal
- Logs remaining API quota on each run

### `models/player_baseline.py` — WORKING
- Builds per-pitcher historical composites from 2024+2025 data
- **2-year weighted averages**: recent year (2025) weighted 2x over 2024
- **Reliability Score (0-100)**: based on total IP, seasons of data, K/9 consistency, xFIP consistency
- **Trend arrows**: UP / DOWN / STABLE / NEW — compares current season to historical average
- Uses fuzzy name matching (SequenceMatcher) to handle minor spelling differences across sources
- Output: 935 pitcher baselines as of April 2026

### `models/ev_calculator.py` — WORKING
- **Pure math layer**: American odds conversion, EV formula, Kelly Criterion (half-Kelly, capped 5%)
- **Poisson model**: converts K/9 + expected IP into probability of going over/under any line
- **Historical blending**: blends current K/9 with historical baseline
  - High-reliability pitchers (90+): ~50% historical, ~50% current
  - Low-reliability/new pitchers: ~70% current, ~30% historical
  - This prevents small-sample flukes from dominating signals
- **Synthetic props fallback**: when no live props exist, generates lines from K/9 for testing
- Flags any prop above 4% EV; saves full results to `ev_signals.csv`

### `alerts/paper_trading.py` — WORKING
- Logs every flagged bet to `data/processed/paper_trades.csv` (called automatically by ev_calculator)
- Sends "BET PLACED" embed to `DISCORD_WEBHOOK_PAPER` channel for each trade
- Sends running P&L summary embed after all bets are placed
- **Auto-resolve**: `python alerts/paper_trading.py auto_resolve` — hits MLB Stats API box scores, marks each PENDING bet WIN or LOSS, fires updated P&L to Discord
- **Manual resolve**: `python alerts/paper_trading.py resolve` — interactive fallback
- **Scheduled**: Windows Task Scheduler runs `resolve_trades.bat` at 11:30 PM ET nightly
- Bankroll starts at $1,000 (set via `BANKROLL` in `.env`)
- Trade columns: date, player, prop_type, side, line, odds, ev, stake, bankroll_before, bankroll_after, result, payout, net, matchup, book

### `alerts/discord_alerts.py` — WORKING
- Sends formatted Discord embed with: player, prop, odds, EV%, model vs implied prob, Kelly stake, game time, tier badge
- **PlaybookIQ score (0-100)**: composite of EV (40pts) + edge (30pts) + xFIP quality (20pts) + sample size (10pts)
- **Tier badges by EV**: Conservative 4-7% (green), Moderate 7-12% (yellow), Aggressive 12-20% (red), Degen 20%+ (purple)
- Embed now shows: K/9 vs History field with trend arrow, Data Reliability score, Blended K/9 used
- Game time looked up live from MLB Stats API
- Webhook URL: `DISCORD_WEBHOOK_CONSERVATIVE` in `.env`
- `fire_alerts_from_signals()` caps at 5 alerts per run to avoid spam

---

## Key Data Files

| File | Updated | Contents |
|------|---------|----------|
| `data/raw/savant_today.csv` | Daily | Today's starters: k_pct, velo, babip |
| `data/raw/pitcher_stats.csv` | Daily | Season leaderboard: k9, xfip, fip, babip |
| `data/raw/team_krates.csv` | Daily | Team K% vs RHP and LHP |
| `data/raw/todays_props.csv` | Daily | Live prop lines from books |
| `data/historical/pitcher_stats_2024.csv` | Once/year | Full 2024 FanGraphs season |
| `data/historical/pitcher_stats_2025.csv` | Once/year | Full 2025 FanGraphs season |
| `data/historical/player_baselines.csv` | Daily | 935 pitcher baselines with trends |
| `data/processed/ev_signals.csv` | Daily | All EV calculations + flags |

---

## Environment Variables (.env)

| Key | Used by | Status |
|-----|---------|--------|
| `ODDS_API_KEY` | odds_api.py | Set |
| `DISCORD_WEBHOOK_CONSERVATIVE` | discord_alerts.py | Set |
| `ANTHROPIC_API_KEY` | (future) Claude analysis | Not set |
| `SUPABASE_URL` + `SUPABASE_KEY` | (future) database | Not set |

---

## Dependencies (all installed)

| Package | Purpose |
|---------|---------|
| `pybaseball` | Statcast + FanGraphs data |
| `anthropic` | Claude AI for analysis (not wired in yet) |
| `requests` | HTTP calls to MLB Stats API and Odds API |
| `supabase` | Database (not connected yet) |
| `apscheduler` | Scheduling (replaced by Task Scheduler — not used) |
| `python-dotenv` | Loads `.env` |
| `discord-webhook` | Sends alerts to Discord |
| `pandas` | Data wrangling |
| `numpy` | Math |
| `scipy` | Poisson distribution for K probability model |

---

## Model Logic (important to understand)

**Why Poisson?** Strikeouts are a count event. Poisson distribution is the correct
model for "how many times does X happen in Y opportunities."

**Why blend historical K/9?** Early-season K/9 is noisy (small sample). A pitcher with
2 years of 9.0 K/9 who has 11.0 in 2 starts is probably not suddenly elite —
blending pulls the estimate back toward what we actually know about them.

**EV formula**: `(model_prob × payout) - (1 - model_prob)`
Positive = profitable long-term. Real edges on live props will be 1-6%, not 20-40%
(synthetic data has inflated edges because lines are set at expected value).

**What the system caught with history**: Kyle Leahy and Zac Gallen were the top
synthetic signals (47%, 39% EV) but both are DOWN 5+ K/9 vs their historical average.
Historical blending correctly downweighted them. The new top signals (Hancock, Irvin,
Boyd) are all pitchers tracking UP vs history — a much stronger foundation.

---

## What to Build Next (rough order)

1. **Railway deployment** — move pipeline execution to the cloud (replaces Windows Task Scheduler)
2. **Claude AI analysis** — prompt Claude with the full signal context, get a plain-English write-up
3. **Batter props** — expand beyond pitchers to hit/HR/RBI props

---

## Coding Rules for This Project

- No coding experience on the owner's side — always explain what you built and why in plain English after writing code
- Test every script immediately after writing it and fix errors before handing back
- Never commit `.env` — it contains real API keys
- Keep scrapers gentle on rate limits (5-second delays minimum)
- Save all raw data to `data/raw/` before any processing
- Prefer simple, readable code over clever one-liners
- After every session, update this file and push to GitHub

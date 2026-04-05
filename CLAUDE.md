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
├── run_playbook.bat      # Double-click to run manually
├── requirements.txt      # All dependencies
├── .env.example          # Template — copy to .env and fill in keys
└── .gitignore            # Keeps .env and data files off GitHub
```

---

## Full Pipeline (main.py — 6 steps)

Run manually: `python main.py`
Scheduled via: **Railway** cron at 10:30 AM ET (`30 14 * * *`) — no Windows Task Scheduler needed.

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
On failure, `send_error_alert()` fires immediately to the health channel.
After all 6 steps, `send_pipeline_summary()` fires to the health channel.

---

## What Has Been Built

### `scrapers/baseball_savant.py` — WORKING
- Fetches today's probable starters from the free MLB Stats API (no key needed)
- For each pitcher: calls `pybaseball.statcast_pitcher()` for last-30-day pitch data
- Computes K%, fastball velocity, BABIP from raw Statcast rows
- Also computes: **spin rate** (fastball RPM), **velocity trend** (last 7d vs 30d avg mph diff), **pitch mix** (% per pitch type as JSON)
- Tries FanGraphs xFIP via `pitching_stats_range()` — fails early in season (empty table), xFIP shows None until ~mid-April
- 2-second delay between requests; pybaseball cache enabled
- Captures pitcher throwing hand (R/L) from MLB Stats API, saved as `throws` column
- **Known quirk**: accented names (e.g. Vásquez) print garbled in Windows terminal — data in CSV is correct

### `scrapers/fangraphs.py` — WORKING
- Team strikeout rates by batter handedness (vs RHP / vs LHP) from Statcast
- Current season pitching leaderboard: xFIP, FIP, K/9, BB/9, BABIP
- Batting team derived from `home_team`/`away_team` + `inning_topbot` (no direct column)
- **Supabase caching**: team K-rates cached daily — skips full Statcast pull if already fetched today (saves 2-5 min on Railway)

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
- **Supabase caching**: baselines cached in `player_baselines_cache` table — skips rebuild if cache under 7 days old (saves 1-2 min on Railway)

### `models/ev_calculator.py` — WORKING
- **Pure math layer**: American odds conversion, EV formula, Kelly Criterion (half-Kelly, capped 5%)
- **Poisson model**: converts K/9 + expected IP into probability of going over/under any line
- **Historical blending** (starts-aware hard floors):
  - 0–3 starts: 92% historical, 8% current
  - 4–6 starts: 80% historical, 20% current
  - 7–9 starts: ramp from reliability-based + early bonus toward normal
  - 10+ starts: normal reliability-based weight (30–50% historical)
- **Velocity trend adjustment**: ±1.5% per mph (last 7d vs 30d avg), capped at ±6%
- **Batter matchup context**: scales expected Ks by opposing lineup K-rate vs pitcher handedness
- **Probability ceiling**: model probability capped at 75% before EV calculation; `prob_capped` column tracks when this fires
- **Low line discount**: lines ≤3.5 → model prob × 0.88; lines ≤2.5 → × 0.80; `low_line_note` column explains when applied
- **Synthetic props fallback**: when no live props exist, generates lines from K/9 for testing
- Flags any prop above 4% EV; saves full results to `ev_signals.csv`
- ev_signals.csv new columns: `velo_trend`, `velo_factor`, `spin_rate`, `pitch_mix`, `prob_capped`, `low_line_note`

### `alerts/paper_trading.py` — WORKING
- Logs every flagged bet to `data/processed/paper_trades.csv` (called automatically by ev_calculator)
- Sends "BET PLACED" embed to `DISCORD_WEBHOOK_PAPER` channel for each trade
- Sends running P&L summary embed after all bets are placed
- **Auto-resolve**: `python alerts/paper_trading.py auto_resolve` — hits MLB Stats API box scores, marks each PENDING bet WIN or LOSS, fires updated P&L to Discord
- **Manual resolve**: `python alerts/paper_trading.py resolve` — interactive fallback
- **Scheduled**: Railway cron service `playbook-resolve` runs at 11:30 PM ET (`30 3 * * *`)
- Bankroll starts at $1,000 (set via `BANKROLL` in `.env`)
- Trade columns: date, player, prop_type, side, line, odds, ev, stake, bankroll_before, bankroll_after, result, payout, net, matchup, book

### `alerts/discord_alerts.py` — WORKING
- **Daily card** (`send_daily_card`): the main alert function — sends all flagged bets as one Discord message (one embed per bet, stacked). Called from ev_calculator.py after each run.
  - Shows: tier badge (🟢🟡🔴🎰), bet title, book/odds/game time, PlaybookIQ bar (▓░ blocks), velo trend emoji + label, K-rate rank sentence, Claude AI narrative
  - Hides: model prob, edge %, EV %, Kelly stake
  - K-rate rank: loads team_krates.csv, ranks opponent among all 30 teams for the pitcher's handedness
  - Claude narrative: 2-3 sentences, casual tone, written for a casual bettor — uses pitcher name, K/9, xFIP, velo trend, opponent context
  - `dry_run=True` prints terminal preview without sending
- **Tier badges by EV**: Conservative 4-7% (🟢), Moderate 7-12% (🟡), Aggressive 12-20% (🔴), Degen 20%+ (🎰)
- **PlaybookIQ score (0-100)**: composite of EV (40pts) + edge (30pts) + xFIP quality (20pts) + sample size (10pts)
- Game time looked up live from MLB Stats API
- Bet alerts → `DISCORD_WEBHOOK_CONSERVATIVE`
- `fire_alerts_from_signals()`: tiered caps — Conservative 5, Moderate 4, Aggressive 3, Degen 1 — sorted by PlaybookIQ descending within each tier
- **Health functions** (go to `DISCORD_WEBHOOK_HEALTH` only — never to bet channels):
  - `send_pipeline_summary(results, runtime_seconds, signal_count, tier_breakdown)` — full run summary after step 6; tier_breakdown renders "🟢 3 · 🟡 2 · 🔴 1 · 🎰 0 — 6 total" in the embed
  - `send_error_alert(step_name, error_message)` — immediate alert on any step failure
  - `send_heartbeat(game_count, pending_trades)` — daily alive-check (call manually or from resolve script)

---

## Key Data Files

| File | Updated | Contents |
|------|---------|----------|
| `data/raw/savant_today.csv` | Daily | Today's starters: k_pct, velo, velo_trend, spin_rate, pitch_mix, babip |
| `data/raw/pitcher_stats.csv` | Daily | Season leaderboard: k9, xfip, fip, babip |
| `data/raw/team_krates.csv` | Daily | Team K% vs RHP and LHP |
| `data/raw/todays_props.csv` | Daily | Live prop lines from books |
| `data/historical/pitcher_stats_2024.csv` | Once/year | Full 2024 FanGraphs season |
| `data/historical/pitcher_stats_2025.csv` | Once/year | Full 2025 FanGraphs season |
| `data/historical/player_baselines.csv` | Daily | 935 pitcher baselines with trends |
| `data/processed/ev_signals.csv` | Daily | All EV calculations + flags + calibration notes |

---

## Environment Variables (.env)

| Key | Used by | Status |
|-----|---------|--------|
| `ODDS_API_KEY` | odds_api.py | Set |
| `DISCORD_WEBHOOK_CONSERVATIVE` | discord_alerts.py | Set |
| `DISCORD_WEBHOOK_PAPER` | paper_trading.py | Set |
| `DISCORD_WEBHOOK_HEALTH` | discord_alerts.py health functions | Set |
| `SUPABASE_URL` + `SUPABASE_KEY` | database.py | Set |
| `BANKROLL` | ev_calculator.py | Set ($1000) |
| `ANTHROPIC_API_KEY` | discord_alerts.py Claude rationale | Not set — falls back to rule-based |

---

## Dependencies (all installed)

| Package | Purpose |
|---------|---------|
| `pybaseball` | Statcast + FanGraphs data |
| `anthropic` | Claude AI for plain-English rationale (falls back if key not set) |
| `requests` | HTTP calls to MLB Stats API and Odds API |
| `supabase` | Database writes |
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

**Why hard floors on early-season blending?** The old smooth ramp still let 1-3 start
samples move the model too much. Hard floors (92% hist for <4 starts, 80% for 4-6)
prevent April noise from generating fake DEGEN signals on live data.

**Why a probability ceiling?** Poisson can output 90%+ confidence on easy lines but
real-world variance (early hook, rain, quick inning) makes near-certainty unreliable.
Capped at 75% to keep Kelly stakes sane.

**Why low line discounts?** Lines of 3.5 and under are highly sensitive to game script.
One early exit or quick inning tanks the result. The model probability is discounted
to reflect that uncertainty: ×0.88 for lines ≤3.5, ×0.80 for lines ≤2.5.

**EV formula**: `(model_prob × payout) - (1 - model_prob)`
Positive = profitable long-term. Real edges on live props will be 1-6%, not 20-40%
(synthetic data has inflated edges because lines are set at expected value).

---

## Deployment

- **Railway — main pipeline**: `python main.py`, cron `30 14 * * *` (10:30 AM ET)
  - Repo: `playbook-edge/playbook`, branch: master
  - Env vars set in Railway dashboard
  - `railway.json` sets builder only — start command set per-service in Railway UI
- **Railway — resolve service** (`playbook-resolve`): `python alerts/paper_trading.py auto_resolve`, cron `30 3 * * *` (11:30 PM ET)
- Ephemeral disk — all persistence goes through Supabase

## Supabase Tables

| Table | Purpose | Refresh |
|-------|---------|---------|
| `ev_signals` | Every flagged bet the model finds | Daily |
| `paper_trades` | Simulated bets + WIN/LOSS results | Per bet |
| `pipeline_runs` | Log of each daily execution | Daily |
| `team_krates_cache` | Team K% vs RHP/LHP | Daily |
| `player_baselines_cache` | 935-pitcher historical composites | Weekly |

## What to Build Next (rough order)

1. **Wire `send_heartbeat`** — call from the resolve script so health channel gets a daily ping after results are processed
2. **Batter props** — expand beyond pitchers to hit/HR/RBI props
3. **Result accuracy tracking** — measure model calibration as the season progresses

---

## Coding Rules for This Project

- No coding experience on the owner's side — always explain what you built and why in plain English after writing code
- Test every script immediately after writing it and fix errors before handing back
- Never commit `.env` — it contains real API keys
- Keep scrapers gentle on rate limits (2-second delays minimum)
- Save all raw data to `data/raw/` before any processing
- Prefer simple, readable code over clever one-liners
- After every session, update this file and push to GitHub

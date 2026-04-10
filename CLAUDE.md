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
├── scripts/              # One-time / manual utility scripts (never scheduled)
│   └── reset_paper_trading.py  # Wipes all bet history for a clean-slate restart
├── logs/                 # Daily pipeline logs (pipeline_YYYY-MM-DD.log)
├── research/             # Notes, experiments, one-off analysis
├── config.py             # All settings loaded from .env
├── main.py               # Full pipeline — runs all 7 steps in order
├── run_playbook.bat      # Double-click to run manually
├── requirements.txt      # All dependencies
├── .env.example          # Template — copy to .env and fill in keys
└── .gitignore            # Keeps .env and data files off GitHub
```

### `scripts/reset_paper_trading.py` — MANUAL ONLY
- Wipes all paper trading history so the system can restart with a fresh $1,000 bankroll
- Clears: `paper_trades`, `ev_signals`, `closing_lines`, `pipeline_runs`, `readiness_history`, `line_movement` in Supabase
- Deletes local: `data/processed/paper_trades.csv`, `data/processed/ev_signals.csv`
- Preserves: `team_krates_cache`, `player_baselines_cache`, `umpire_profiles`, `statcast_pull_log`
- Requires typing `YES` at a safety prompt before anything is deleted
- **Never schedule or add to main.py** — run once manually before a season restart

---

## Full Pipeline (main.py — 7 steps)

Run manually: `python main.py`
Scheduled via: **Railway** cron at 10:30 AM ET (`30 14 * * *`) — no Windows Task Scheduler needed.

```
Step 1    scrapers/baseball_savant.py   →  data/raw/savant_today.csv
Step 2    scrapers/fangraphs.py         →  data/raw/pitcher_stats.csv
                                            data/raw/team_krates.csv
Step 2.5  scrapers/umpire_scraper.py    →  Supabase: umpire_profiles (weekly refresh)
Step 2.6  scrapers/weather_scraper.py   →  data/raw/weather_today.csv
Step 3    scrapers/historical_stats.py  →  data/historical/pitcher_stats_2024.csv
                                            data/historical/pitcher_stats_2025.csv
                                            data/historical/pitcher_stats_all.csv
Step 4    models/player_baseline.py     →  data/historical/player_baselines.csv
Step 5    scrapers/odds_api.py          →  data/raw/todays_props.csv
Step 6    models/ev_calculator.py       →  data/processed/ev_signals.csv
                                            → fires Discord alerts automatically
```

Each step is isolated — one failure doesn't stop the rest.
On failure, `send_error_alert()` fires immediately to the health channel.
After all 7 steps, `send_pipeline_summary()` fires to the health channel.

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
- **avg_ip**: calls `statsapi.mlb.com/api/v1/people/{id}?hydrate=stats(...)` per starter to get current 2026 IP/GS; stored as `avg_ip` in savant_today.csv
- **hist_avg_ip**: weighted 2024+2025 average IP/start from pitcher_stats_all.csv; stored alongside `avg_ip` for blending
- **curr_gs**: current 2026 games started, also from the same MLB Stats API call; stored as `curr_gs` in savant_today.csv. Used by ev_calculator as the blending weight source when FanGraphs is unavailable.
- **Known quirk**: accented names (e.g. Vásquez) print garbled in Windows terminal — data in CSV is correct

### `scrapers/fangraphs.py` — WORKING
- Team strikeout rates by batter handedness (vs RHP / vs LHP) from Statcast
- Current season pitching leaderboard: xFIP, FIP, K/9, BB/9, BABIP
- Batting team derived from `home_team`/`away_team` + `inning_topbot` (no direct column)
- **Supabase caching**: team K-rates cached daily — skips full Statcast pull if already fetched today (saves 2-5 min on Railway)
- **FanGraphs 403 handling** (added 2026-04-07): FanGraphs blocks server IPs (Railway) on `leaders-legacy.aspx`. `build_pitcher_leaderboard()` now has three-layer fallback: (1) same-day file cache — if `pitcher_stats.csv` was written today, skip the fetch entirely; (2) pybaseball fetch with WARNING on failure instead of crash; (3) use previous day's CSV if fetch fails. Step logs `Done` instead of `FAIL`. ev_calculator's hist_xfip fallback handles any missing xFIP data downstream.

### `scrapers/historical_stats.py` — WORKING
- Pulls full 2024 and 2025 FanGraphs season stats via `pitching_stats(year, year, qual=0)`
- Skips re-fetch if file already exists (cached to disk)
- Saves per-year CSVs + combined `pitcher_stats_all.csv`
- Run once to seed; re-run at end of each season to add the new year
- **2026-04-07 fix**: `pitcher_stats_2024.csv`, `pitcher_stats_2025.csv`, and `pitcher_stats_all.csv` are now committed directly to the repo (`.gitignore` exceptions added). FanGraphs was blocking the daily re-fetch on Railway (ephemeral disk = files never persisted). Since those seasons are over and the data never changes, committing them is the right permanent fix. To add a new season, pull it locally and commit the new CSV.

### `scrapers/odds_api.py` — WORKING (needs API key)
- Fetches today's MLB events, then per-event pitcher strikeout props
- Books: DraftKings, FanDuel, BetMGM
- Requires `ODDS_API_KEY` in `.env` (the-odds-api.com — free tier: 500 req/month)
- Props post around 9-10am ET on game days; empty file before then is normal
- **Quota monitoring**: after every API call, reads `x-requests-remaining` from the response header. Fires `send_error_alert()` to the health channel at two thresholds: warning at <100 remaining, critical (🚨) at <25. Final remaining count is written to `data/raw/odds_api_quota.txt` for the pipeline log.
- `get_todays_events()` and `get_event_props()` both return remaining quota alongside their data so the scraper always has the freshest count.

### `models/player_baseline.py` — WORKING
- Builds per-pitcher historical composites from 2024+2025 data
- **2-year weighted averages**: recent year (2025) weighted 2x over 2024
- **Reliability Score (0-100)**: based on total IP, seasons of data, K/9 consistency, xFIP consistency
- **Trend arrows**: UP / DOWN / STABLE / NEW — compares current season to historical average
- Uses fuzzy name matching (SequenceMatcher) to handle minor spelling differences across sources
- Output: 935 pitcher baselines as of April 2026
- **Supabase caching**: baselines cached in `player_baselines_cache` table — skips rebuild if cache under 7 days old (saves 1-2 min on Railway)

### `models/ev_calculator.py` — WORKING
- **Prop types supported**: `pitcher_strikeouts` (Over/Under total Ks) and `pitcher_innings` (Over/Under innings pitched)
- **Pure math layer**: American odds conversion, EV formula, Kelly Criterion (half-Kelly, capped 5%)
- **Poisson model**: converts K/9 + expected IP into probability of going over/under any strikeout line
- **Normal distribution model**: for `pitcher_innings` props, uses pitcher's avg IP/start + a 1.2-inning standard deviation to compute over/under probability. Replaces Poisson (which is for counts, not continuous innings)
- **Per-pitcher innings**: uses `avg_ip` from savant_today.csv (current 2026 IP/GS from MLB Stats API) blended against `hist_avg_ip` (2yr historical) — same early-season schedule as K/9 blending. Replaces the old fixed 5.5-inning default.
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
- **EV threshold**: 2% (changed from 4%) — generates more signals for paper trading validation
- **ev_suspect flag**: signals where EV > 25% on live props are flagged as suspect — logged to Supabase but excluded from Discord alerts and paper trading. Prevents inflated early-season data from generating fake Degen signals. (Raised from 18% on 2026-04-09 — 18% was too aggressive early in the season, blocking legitimate Moderate/Aggressive signals.)
- **pitcher_stats.csv is optional (2026-04-10)**: FanGraphs is 403-blocked on Railway so `pitcher_stats.csv` never gets written on Railway's ephemeral disk. The EV calculator previously exited immediately if the file was missing (0 signals every day). Fixed: `savant_today.csv` is the only hard requirement. If `pitcher_stats.csv` is absent, `stats_df` is set to an empty DataFrame and `build_ev_signals()` falls through to the Statcast path automatically.
- **SIGNAL PIPELINE SUMMARY diagnostic**: printed at the end of every `run()` — shows props loaded, total rows generated, EV range, above-threshold count, ev_suspect filtered, duplicate filtered, remaining for alerts, and Forced Degen status.
- **duplicate flag**: after all signals are generated, props with the same pitcher + prop_type + side + line appearing from multiple books are deduplicated. Only the row with the best American odds (highest payout) is kept for alerts and paper trading. Lower-payout duplicates get `duplicate=True` — still logged to Supabase for reference but excluded from Discord and paper trades.
- **xFIP fallback (Fix 2)**: when a pitcher's current-season xFIP is None (common all of April), `build_ev_signals()` falls back to `hist_xfip` from `player_baselines.csv`, then to `4.20` (MLB league average) as a last resort. Prevents the PlaybookIQ xFIP component from scoring a flat neutral when historical data is available. A scope report is printed on each run showing how many of today's starters needed the fallback.
- **low_history flag**: pitchers with fewer than 8 total GS across 2024+2025 → 95% historical weight, 0.65 probability cap. Protects against injury returnees and callups with tiny historical samples.
- **Forced Degen**: if no natural 20%+ EV signal exists, the top-ranked signal is force-badged as 🎰 Degen so every daily card has at least one headline pick
- **Weather integration**: `build_ev_signals()` accepts `weather_df`; `_weather_cols()` helper maps home team to wind/temp/precip. Weather line appears in Discord embeds.
- **Innings cap detection**: if a pitcher has 3+ FanGraphs-verified starts and their blended IP/start is >1.0 inning below their historical baseline, `innings_capped = True` is set and `ip_per_start` is reduced by another 0.5 innings. Protects against overvaluing K props on managed/limited arms. When triggered, Claude narrative also receives a note: "pitcher appears to be on an innings limit."
- **Team K-rate PA threshold**: after `lookup_opp_krate()`, checks `pa_vs_rhp` or `pa_vs_lhp` for the opposing team. If <150 PA, blends the current K-rate 30% current / 70% league average (22.5%) to dampen early-season sample noise. Stored in `matchup_pa_count`.
- ev_signals.csv columns (54 total): original 25 + `velo_trend`, `velo_factor`, `spin_rate`, `pitch_mix`, `throws`, `prob_capped`, `low_line_note`, `umpire_name`, `umpire_adjustment`, `kelly_cap_applied`, `low_history`, `ev_suspect`, `weather_wind_label`, `weather_wind_factor`, `weather_temp_f`, `weather_precip_pct`, `duplicate`, `innings_capped`, `matchup_pa_count` + `iq_reliability`, `iq_alignment`, `iq_market`, `iq_tier`, `iq_clarity`, `playbookiq` + `xfip_source` + `park_name`, `park_k_factor`, `park_k_label`

### `scrapers/park_factors.py` — WORKING
- Static dictionary of all 30 MLB stadiums keyed by full team name (matching savant_today.csv / matchup strings)
- Each entry: `park_name`, `general_factor` (3yr avg run park factor, centered at 100), `k_factor` (K-specific, centered at 100), `dome` (bool), `altitude_ft`
- `get_park_k_adjustment(home_team)` → `(k_multiplier, park_label, k_factor)`:
  - k_factor >108 → 1.04 "K-boosting park"; 103-108 → 1.02 "Slight K boost"; 97-103 → 1.00 "Neutral park"; 92-97 → 0.98 "Slight K suppressor"; <92 → 0.96 "K-suppressing park"
  - Coors Field (altitude >4000 ft) → 0.94 "Coors — significant K suppressor"
- `home_team_from_matchup(matchup)` — extracts home team from "Away @ Home" string
- Notable parks: Tropicana Field (109, K-boosting), Seattle T-Mobile (104), Fenway/Oracle/Petco (95-96, K-suppressing), Coors (88, extreme suppressor)
- Wired into ev_calculator as Layer 4.5 — multiplier applied to adjusted_k9 after batter matchup, before umpire adjustment
- Three columns in ev_signals.csv: `park_name`, `park_k_factor`, `park_k_label`
- Park factor aligned with bet direction adds up to +3 pts to PlaybookIQ Signal Alignment component
- Park line shown in Discord daily card embeds: 🏟️ {park_name} — {label} (K-factor: {value})
- No API calls — fully static, no rate limiting needed
- **Supabase**: `park_name` (TEXT), `park_k_factor` (INTEGER), `park_k_label` (TEXT) columns added to ev_signals table 2026-04-07

### `scrapers/weather_scraper.py` — WORKING
- **Open-Meteo API** (free, no key): hourly wind speed/direction, temperature, precipitation forecasts keyed on ballpark lat/lon
- `STADIUM_COORDS`: 30 MLB stadiums with lat/lon, center-field direction (degrees), dome flag
- `get_game_weather(home_team_code, game_datetime_utc)` — calls Open-Meteo for the closest hour to first pitch
- `calculate_wind_adjustment(wind_speed_mph, wind_direction_deg, cf_direction_deg, is_dome)` — returns `(wind_factor, wind_label)`
  - Wind blowing out 15+ mph: +0.25 factor; 10-15 mph out: +0.15
  - Wind blowing in 10-15 mph: -0.15; 15+ mph in: -0.25
  - Within 45 degrees of center-field axis = "out" or "in"; else neutral
- `get_todays_weather(date_str=None)` — calls MLB Stats API schedule to get today's games + home teams, runs weather per game, saves `data/raw/weather_today.csv`
- Columns saved: `home_team`, `wind_speed_mph`, `wind_direction_deg`, `wind_label`, `wind_factor`, `temperature_f`, `precip_pct`, `is_dome`
- Pipeline step 2.6 — runs between umpires and historical stats
- ev_calculator loads `weather_today.csv` and passes it to `build_ev_signals()` as `weather_df`; weather columns written to `ev_signals.csv` and Supabase

### `scrapers/umpire_scraper.py` — WORKING
- **Part 1 — profiles (weekly)**: fetches umpire zone tendency data from `umpscorecards.com/api/umpires?season={year}`
- Computes `zone_size_pct` (0-100): percentile rank of how pitcher-friendly each umpire's zone is. Derived by INVERTING the rank of `total_run_impact_mean` — lower run impact = fewer runs from missed calls = more pitcher-friendly = higher zone_size_pct.
- Saves 100+ profiles to Supabase `umpire_profiles` table; weekly refresh (re-fetches if cache is older than 7 days)
- **Part 2 — today's assignments**: calls MLB Stats API `?sportId=1&date={TODAY}&hydrate=officials` to find the home plate umpire for each game
- Matches to profiles by normalized full name (umpscorecards has no MLB umpire IDs)
- Returns a dict keyed by `'{away} @ {home}'` matchup string — same format as ev_signals.csv matchup column
- **K factor**: zone_pct > 60 → +3% K boost; zone_pct < 40 → -3% K trim; else neutral

### `alerts/paper_trading.py` — WORKING
- Logs every flagged bet to `data/processed/paper_trades.csv` (called automatically by ev_calculator)
- Sends "BET PLACED" embed to `DISCORD_WEBHOOK_PAPER` channel for each trade
- Sends running P&L summary embed after all bets are placed
- **Auto-resolve**: `python alerts/paper_trading.py auto_resolve` — hits MLB Stats API box scores, marks each PENDING bet WIN or LOSS, fires updated P&L to Discord
  - Checks game status first: Final → resolve; In Progress → skip; Postponed/Suspended/Cancelled → increment postpone_count, health alert at 3
  - **UTC date fix (2026-04-09)**: Railway runs in UTC. At 11:30 PM ET the UTC clock reads 3:30 AM the next day — `datetime.now()` was returning tomorrow's date, making today's bets look stale and marking them EXPIRED before resolution. All date logic now uses `datetime.now(ET)` with `ET = timezone(timedelta(hours=-4))`. The log prints both ET date and UTC time for verification.
  - Captures closing odds from Odds API and logs CLV (closing_implied_prob - opening_implied_prob) per resolved bet
  - Avg CLV reported in nightly P&L Discord embed
- **Manual resolve**: `python alerts/paper_trading.py resolve` — interactive fallback
- **Scheduled**: Railway cron service `playbook-resolve` runs at 11:30 PM ET (`30 3 * * *`)
- Bankroll starts at $1,000 (set via `BANKROLL` in `.env`)
- Trade columns: date, player, prop_type, side, line, odds, ev, stake, bankroll_before, bankroll_after, result, payout, net, matchup, book, postpone_count

### `alerts/discord_alerts.py` — WORKING
- **Daily card** (`send_daily_card`): the main alert function — sends all flagged bets as one Discord message (one embed per bet, stacked). Called from ev_calculator.py after each run.
  - Shows: tier badge (🟢🟡🔴🎰), bet title, book/odds/game time, PlaybookIQ star rating (⭐–⭐⭐⭐⭐⭐) + score + label, velo trend emoji + label, K-rate rank sentence, park context line, Claude AI narrative
  - Hides: model prob, edge %, EV %, Kelly stake
  - K-rate rank: loads team_krates.csv, ranks opponent among all 30 teams for the pitcher's handedness
  - Park line: `🏟️ {park_name} — {park_k_label} (K-factor: {park_k_factor})` shown between weather and narrative
  - Claude narrative: 2-3 sentences, casual tone, written for a casual bettor — uses pitcher name, K/9, xFIP, velo trend, opponent context
  - `dry_run=True` prints terminal preview without sending
- **Tier badges by EV**: Conservative 4-7% (🟢), Moderate 7-12% (🟡), Aggressive 12-20% (🔴), Degen 20%+ (🎰)
- **PlaybookIQ score (0-100)**: redesigned 2026-04-07 to measure confidence and trustworthiness, not model-vs-book disagreement. Five components (raw max = 103, rescaled ×100/103):
  - **Data Reliability (25 pts)**: pitcher's `hist_reliability` score (80+→25, 60-79→18, 40-59→10, <40→4). Capped at 8 if `low_history=True`.
  - **Signal Alignment (max 28 pts, rescaled)**: counts contextual factors supporting the bet direction — velo trend aligned (+6), umpire adjustment aligned (+6), team K-rate matchup aligned (+6), xFIP quality <3.20→+4 / 3.20-3.80→+2, K prop wind neutral (+3), park factor aligned with bet direction (+3). Max raw = 28.
  - **Market Reasonableness (25 pts)**: edge 2-5%→25, 5-8%→20, 8-12%→12, 12-18%→5, 18%+→0. `ev_suspect=True`→0 always.
  - **Tier Confidence (15 pts)**: Conservative→15, Moderate→10, Aggressive→4, Degen→0.
  - **Bet Type Clarity (10 pts)**: Over with K/9>9.0 and line>5.5→10; Under with K/9<7.5 and line<5.5→10; within 1.5 Ks of expected total→7; low line (≤3.5)→2; other→5.
  - Conservative signals consistently outscore Degen signals (verified: Conservative ~70-76, Degen ~29-51 on 2026-04-07 data).
  - All five component scores stored as separate columns in ev_signals.csv: `iq_reliability`, `iq_alignment`, `iq_market`, `iq_tier`, `iq_clarity`, `playbookiq`.
  - Components computed in `ev_calculator.py:build_ev_signals()`, stored pre-computed. Discord reads the stored value rather than recalculating.
  - **`iq_*` columns confirmed live in Supabase** (migration ran 2026-04-07).
- **`playbookiq_stars(score) -> tuple`**: converts 0-100 score to star display + label for Discord embeds.
  - 90+→⭐⭐⭐⭐⭐ Elite, 75-89→⭐⭐⭐⭐ Strong, 60-74→⭐⭐⭐ Good, 45-59→⭐⭐ Fair, <45→⭐ Weak
  - Embed IQ line: `{stars}  **{score}**  {label}   {trend_emoji}  {trend_label}`
- **`calculate_playbook_iq_components(signal: dict) -> dict`**: computes all five IQ components from a signal dict. Returns `{iq_reliability, iq_alignment, iq_market, iq_tier, iq_clarity, playbookiq}`.
- **`calculate_playbook_iq(signal: dict) -> int`**: reads pre-computed `playbookiq` from the signal dict; falls back to `calculate_playbook_iq_components()` only if not present.
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
| `data/raw/savant_today.csv` | Daily | Today's starters: k_pct, velo, velo_trend, spin_rate, pitch_mix, babip, throws, avg_ip, hist_avg_ip, curr_gs |
| `data/raw/weather_today.csv` | Daily | Game-day weather per home team: wind_label, wind_factor, temp_f, precip_pct |
| `data/raw/pitcher_stats.csv` | Daily | Season leaderboard: k9, xfip, fip, babip |
| `data/raw/team_krates.csv` | Daily | Team K% vs RHP and LHP |
| `data/raw/todays_props.csv` | Daily | Live prop lines from books |
| `data/raw/odds_api_quota.txt` | Daily | Remaining Odds API requests this month (written by odds_api.py, read by main.py for pipeline_runs log) |
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
| `DISCORD_WEBHOOK_URL` | config.py (legacy — not actively used) | Set |
| `SUPABASE_URL` + `SUPABASE_KEY` | database.py | Set |
| `BANKROLL` | paper_trading.py, ev_calculator.py | Set ($1000) |
| `ANTHROPIC_API_KEY` | discord_alerts.py Claude rationale | Set |
| `SPORT` | config.py (optional — defaults to `baseball_mlb`) | Optional |
| `MIN_EDGE_PERCENT` | config.py (optional — defaults to `5`) | Optional |

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
- **Railway — snapshot service** (`playbook-snapshot`): `python scrapers/odds_api.py snapshot`, cron `30 22 * * *` (6:30 PM ET). Fetches current prop lines for all games starting within the next 3 hours, saves to `data/raw/props_gameday_snapshot.csv`, then immediately runs `capture_line_movement()` to write opening vs snapshot comparison to Supabase `line_movement` table.
- **Railway — readiness dashboard** (`playbook-readiness`): `python alerts/readiness_dashboard.py`, cron `0 13 * * 0` (9am ET Sundays)
- Ephemeral disk — all persistence goes through Supabase

## Supabase Tables

| Table | Purpose | Refresh |
|-------|---------|---------|
| `ev_signals` | Every flagged bet the model finds | Daily |
| `paper_trades` | Simulated bets + WIN/LOSS results | Per bet |
| `closing_lines` | Closing odds captured at resolve time for CLV tracking | Per resolved bet |
| `pipeline_runs` | Log of each daily execution | Daily |
| `team_krates_cache` | Team K% vs RHP/LHP (running PA/K totals for incremental updates) | Daily (incremental) |
| `statcast_pull_log` | Tracks last Statcast pull date to enable incremental delta fetches | Daily |
| `player_baselines_cache` | 935-pitcher historical composites | Weekly |
| `umpire_profiles` | Umpire zone tendency data from umpscorecards.com | Weekly |
| `line_movement` | Opening vs 6:30 PM snapshot odds comparison for each active paper trade | Per snapshot run |
| `readiness_history` | Weekly snapshot of verdict, CLV, ROI, win rate, bankroll, checklist score | Weekly (Sundays) |

All tables confirmed live and accepting writes as of 2026-04-07.

`ev_signals` schema history:
- 2026-04-06: added 16 columns (`velo_trend`, `velo_factor`, `spin_rate`, `pitch_mix`, `throws`, `prob_capped`, `low_line_note`, `umpire_name`, `umpire_adjustment`, `kelly_cap_applied`, `low_history`, `ev_suspect`, `weather_wind_label`, `weather_wind_factor`, `weather_temp_f`, `weather_precip_pct`) + `duplicate`
- 2026-04-07 (morning): `innings_capped` (BOOLEAN), `matchup_pa_count` (INTEGER)
- 2026-04-07 (session): `iq_reliability`, `iq_alignment`, `iq_market`, `iq_tier`, `iq_clarity`, `playbookiq` (all INTEGER) — **migration confirmed live**
- 2026-04-07 (session): `xfip_source` (TEXT), `park_name` (TEXT), `park_k_factor` (INTEGER), `park_k_label` (TEXT) — **migration confirmed live**

`pipeline_runs` notes field now includes `odds_api_quota:N` appended by `log_pipeline_run()` — written by `main.py` reading `data/raw/odds_api_quota.txt` after the odds scraper step. No schema migration needed.

`readiness_history` columns: `date`, `verdict`, `avg_clv_overall`, `avg_clv_conservative`, `roi_pct`, `total_bets`, `win_rate`, `bankroll_current`, `checklist_complete_count` (0–4 score counting GO conditions met). Written by `save_readiness_snapshot()` in `readiness_dashboard.py` on every run. Read back to render the 4-week trend section in the Discord embed.

## Roadmap

1. **Real-money readiness dashboard** ✓ BUILT
   `alerts/readiness_dashboard.py` — weekly Discord embed to health channel. Shows verdict (GO/MONITOR/NOT YET/HOLD), avg CLV by tier, win rate vs break-even, ROI, bankroll, and a **4-week trend table** (date / verdict / CLV / ROI / checklist score). Every run writes a row to `readiness_history` in Supabase so the trend accumulates over the season. Railway service `playbook-readiness`, cron `0 13 * * 0` (9am ET Sundays). Run manually: `python alerts/readiness_dashboard.py` or `... preview` for terminal-only.

2. **Wire `send_heartbeat`** ✓ ALREADY DONE
   Called at the end of `auto_resolve` in both paths (bets resolved + no pending bets). Sends W/L record, pending count, game count to health channel. Lives in `discord_alerts.py:send_heartbeat()`.

3. **Weather scraper** (`scrapers/weather_scraper.py`) — BUILT
   Fetches game-day weather for each ballpark using Open-Meteo API (free, no key). Runs as Step 2.6 in pipeline (after umpires, before historical). Saves `data/raw/weather_today.csv`. Wind factor baked into ev_signals and Discord embeds. See full details in `scrapers/weather_scraper.py` section below.

4. **Pitcher hand-off / innings cap detection** ✓ BUILT
   `innings_capped` flag in ev_calculator.py: fires when pitcher has 3+ FanGraphs-verified starts and blended IP/start is >1.0 inning below historical baseline. Reduces ip_per_start by 0.5 more and injects a note into the Claude narrative. `matchup_pa_count` also added: blends team K-rate toward league average (22.5%) when opposing team has <150 PA vs the pitcher's handedness.

5. **Line movement tracking** ✓ BUILT
   `scrapers/odds_api.py snapshot` → `alerts/paper_trading.py capture_line_movement()`. Railway service `playbook-snapshot`, cron `30 22 * * *` (6:30 PM ET). Saves `data/raw/props_gameday_snapshot.csv` and writes to Supabase `line_movement` table. `movement_direction` is `toward` (market agrees), `against` (market disagrees), or `flat`. Run manually: `python scrapers/odds_api.py snapshot`.

6. **Park factors** ✓ BUILT (2026-04-07)
   `scrapers/park_factors.py` — static 30-park dictionary. Wired into ev_calculator as Layer 4.5. Multiplier applied to adjusted_k9 after batter matchup. Park line in Discord embeds. +3 pts to PlaybookIQ Signal Alignment when park aligns with bet direction.

7. **FanGraphs resilience** ✓ BUILT (2026-04-07)
   FanGraphs blocks Railway IPs at the network level (confirmed 403 IP-block, not user-agent). Three mitigations:
   - Browser headers patched onto all pybaseball FanGraphs calls (no-op for now but ready if policy changes)
   - `calculate_xfip_from_statcast()` in baseball_savant.py computes xFIP from Statcast pitch rows; saved as `xfip_statcast` in savant_today.csv
   - Four-source xFIP fallback chain: fangraphs → statcast_calculated → historical → league_average (4.20). Source tracked in `xfip_source` column.
   - `curr_gs` (games started) now saved to savant_today.csv from MLB Stats API — ev_calculator uses it for blending weight when FanGraphs is unavailable
   - Model runs fully on MLB Stats API + Statcast alone if FanGraphs is completely down

8. **PlaybookIQ redesign** ✓ BUILT (2026-04-07)
   Replaced 4-component EV-rewarding formula with 5-component confidence/trustworthiness formula. Conservative signals now outscore Degen signals (verified: Conservative ~70-76, Degen ~29-51). Components stored per-signal for auditability. Star rating display (⭐ to ⭐⭐⭐⭐⭐) replaces progress bar in Discord embeds.

9. **Batter props**
   Expand beyond pitchers to HR, hits, RBI props. Requires new scrapers, new model logic, and new prop types. Later-season project once pitcher-side model is validated.

## Kelly Criterion — Tier-Based Caps (updated 2026-04-05)

Stakes are no longer a flat 5% cap. Each tier has its own maximum:

| Tier | EV Range | Max % of bankroll | Max $ on $1,000 |
|------|----------|-------------------|-----------------|
| Conservative | 4–7% | 3% | $30 |
| Moderate | 7–12% | 2% | $20 |
| Aggressive | 12–20% | 0.5% | $5 |
| Degen | 20%+ | flat $7 (Kelly ignored) | $7 |

Logic: higher claimed EV on synthetic/early-season data is a red flag, not a green light. Degen signals on synthetic props can show 40-60% EV which is meaningless — the $7 flat stake keeps them logged in paper trading without risking real bankroll. Conservative 4-7% signals on live props are the most credible, so they get the most room.

`kelly_cap_applied` column in ev_signals.csv: `True` when the cap was hit and Kelly wanted more, `False` when Kelly naturally came in under (or Degen flat override).

---

---

## Architecture Overview (Plain English)

### What Playbook Does

Playbook is an automated baseball betting research tool. Every morning during the MLB season, it wakes up, collects data from five different sources, runs a probability model, compares the model's opinion to what the sportsbooks are offering, and if it finds a bet where the math says we have an edge, it sends a formatted alert to Discord. At night, it checks the game results and scores each bet as a WIN or LOSS. Everything is tracked so we can measure whether the model is actually right over time.

---

### The Daily Pipeline (7 Steps)

**Step 1 — Baseball Savant** (`scrapers/baseball_savant.py`)
Finds today's probable starting pitchers from the MLB Stats API (free, no key needed). For each pitcher, pulls their last 30 days of Statcast data: strikeout rate, fastball velocity, velocity trend (is he throwing harder or softer than his average this month?), spin rate, and pitch mix. Also fetches their current 2026 innings-per-start average and their historical innings-per-start from 2024-2025 for blending. Saves everything to `savant_today.csv`.

**Step 2 — FanGraphs** (`scrapers/fangraphs.py`)
Pulls two things: (1) a season-long pitching leaderboard with xFIP and K/9 for every pitcher with meaningful innings, and (2) how often each of the 30 MLB teams strikes out against right-handed vs left-handed pitching. The team K-rates are pulled incrementally — it only fetches new days since the last pull and adds those to the running totals, instead of re-pulling the whole season every day. Both are saved to CSV and cached in Supabase.

**Step 2.5 — Umpire Profiles** (`scrapers/umpire_scraper.py`)
Fetches umpire zone tendency data from umpscorecards.com. Computes a `zone_size_pct` score for each umpire (0-100) reflecting how pitcher-friendly their zone is — high score means tight zone, more strikeouts; low score means a wide zone, fewer strikeouts. Also calls the MLB Stats API to find today's home plate umpire assignment for each game. Profiles are cached in Supabase and only re-fetched weekly (umpire tendencies don't change day-to-day).

**Step 3 — Historical Stats** (`scrapers/historical_stats.py`)
Pulls the full 2024 and 2025 FanGraphs season stats for every pitcher. These are cached to disk and only re-fetched at the end of each season. Combined into `pitcher_stats_all.csv` which is the foundation for the baseline model.

**Step 4 — Player Baselines** (`models/player_baseline.py`)
Builds a composite historical profile for each pitcher: 2-year weighted K/9 (2025 counts double), reliability score (0-100 based on innings, seasons, consistency), and a trend arrow (UP/DOWN/STABLE/NEW comparing current season to historical average). Cached in Supabase for up to 7 days.

**Step 5 — Odds API** (`scrapers/odds_api.py`)
Fetches live prop lines from DraftKings, FanDuel, and BetMGM for today's pitcher strikeout props. The lines are usually posted around 9-10am ET. This step costs API quota (500 requests/month on the free tier) so it runs once per day at 10:30am when the pipeline fires. After every API call, remaining quota is checked from the response headers — health alerts fire at <100 (warning) and <25 (critical 🚨). Final quota is written to `data/raw/odds_api_quota.txt` and appended to the `pipeline_runs` Supabase record via `main.py`.

**Step 6 — EV Calculator** (`models/ev_calculator.py`)
This is where all the data comes together. Two prop types are supported: `pitcher_strikeouts` and `pitcher_innings`. For each live prop line, it: (1) builds a blended K/9 and IP estimate from current + historical data, (2) applies adjustments for velocity trend, opposing lineup K-rate, and umpire zone (K props only), (3) uses a Poisson distribution for strikeout lines or a Normal distribution for innings lines to compute the probability of going over or under, (4) compares that probability to the implied probability in the book's odds, (5) calculates EV and Kelly stake. If EV is above 4%, the signal is flagged and sent to Discord.

**Step 7 — Auto-Resolve** (`alerts/paper_trading.py auto_resolve`) — runs separately at 11:30 PM ET
Checks every pending bet against the MLB Stats API box scores. If the game is Final, marks the bet WIN or LOSS. If the game is Postponed/Suspended/Cancelled, increments the postpone counter (and sends a health alert if a bet has been postponed 3+ times). Captures closing odds from the Odds API for CLV tracking. Sends a P&L summary to Discord.

---

### The Model in Detail (9 Layers)

**Layer 1 — K/9 Blending**
Uses the current 2026 K/9 from Statcast but blends it with the 2-year historical baseline using hard floors based on starts pitched. The fewer starts a pitcher has, the more we trust their history over their current small sample:
- 0-3 starts: 92% historical, 8% current
- 4-6 starts: 80% historical, 20% current
- 7-9 starts: gradual ramp from 80% toward normal weighting
- 10+ starts: normal reliability-based weight (roughly 30-50% historical depending on reliability score)

**Layer 2 — Innings Estimate**
How many innings will the starter pitch? Uses the same blending schedule as K/9 — current 2026 IP/GS (from MLB Stats API) blended against 2-year historical. Early in the season, a pitcher with 2 starts might have 2.8 IP/start due to a rough outing — blending with their historical 5.8 pulls the estimate back toward reality. Replaces the old fixed 5.5-inning default.

**Layer 3 — Velocity Trend**
If a pitcher's average fastball velocity over the last 7 days is different from his 30-day average, the model adjusts K probability: +1.5% per MPH up, -1.5% per MPH down, capped at +/-6%. A guy throwing 1 MPH harder recently gets a 1.5% K boost; one who's clearly lost velocity gets a trim.

**Layer 4 — Batter Matchup**
Scales expected strikeouts by the opposing team's K-rate vs the pitcher's handedness (R or L). If a right-handed pitcher is facing a team that strikes out 28% of the time against right-handers (vs a 22% league average), their expected Ks scale up proportionally.

**Layer 5 — Umpire Adjustment**
If the home plate umpire has a historically tight zone (above 60th percentile zone_size_pct), the model applies a +3% K boost. If he has a historically wide zone (below 40th percentile), a -3% trim. Neutral zone = no adjustment.

**Layer 6 — Probability Model**
Two models are used depending on prop type:
- **Strikeout props**: Poisson distribution. Takes blended K/9 and expected innings, computes an expected K total, then calculates the probability of going over or under any specific line (e.g., over 5.5 Ks). Poisson is the right math for count events — how many times does X happen in Y chances.
- **Innings props**: Normal distribution. Uses the pitcher's blended avg IP/start with a default standard deviation of 1.2 innings to model the probability of going over or under an innings line (e.g., over 5.5 IP). Innings pitched is a continuous value, so Normal distribution fits better than Poisson.

**Layer 7 — Safety Filters**
Two filters prevent the model from being overconfident on structurally risky bets:
- Probability ceiling: capped at 75% before EV calculation. Even if the math says 91% confidence, we cap it — real-world variance (early hook, rain, quick inning) makes near-certainty unreliable.
- Low line discount: lines of 3.5 Ks or under are heavily sensitive to game script. Model prob is discounted: x0.88 for lines ≤3.5, x0.80 for lines ≤2.5.

**Layer 8 — EV Calculation**
`EV = (model_prob × payout) - (1 - model_prob)`. Any result above 4% (0.04) gets flagged. Real edges on live props will typically be 1-6%, not 20-40% (the latter only appear in testing with synthetic data where lines are set at expected value).

**Layer 9 — Kelly Sizing**
Half-Kelly criterion, capped at 5% of bankroll. Sizes the bet proportionally to the edge — bigger edge, bigger stake. Half-Kelly is used instead of full Kelly to reduce variance.

---

### Data Storage (Supabase)

All persistence goes through Supabase because Railway's filesystem is ephemeral — files written during a run are gone after the container restarts.

| Table | What's in it |
|-------|-------------|
| `ev_signals` | Every flagged bet the model finds, every day — player, line, odds, EV, model prob, K/9 inputs, umpire adjustment |
| `paper_trades` | Every simulated bet placed, with result (WIN/LOSS/PENDING) and P&L |
| `closing_lines` | Closing odds captured at resolve time; used to calculate CLV |
| `team_krates_cache` | Running PA and K totals per team per handedness; just 30 rows, updated incrementally each day |
| `statcast_pull_log` | Tracks what date we last pulled Statcast data; enables incremental delta fetches |
| `player_baselines_cache` | 935-pitcher historical composites; rebuilt weekly |
| `umpire_profiles` | Umpire zone tendency data; rebuilt weekly |
| `pipeline_runs` | Log of every daily pipeline execution with step pass/fail counts |

---

### Output Layer (Discord)

Three Discord channels receive different types of messages:

**Bet channel** (`DISCORD_WEBHOOK_CONSERVATIVE`): The main alert channel. Each flagged bet appears as a formatted embed with: tier badge (Conservative/Moderate/Aggressive/Degen), PlaybookIQ score (0-100 composite), book and odds, game time, velocity trend emoji, K-rate rank sentence, and a 2-3 sentence Claude AI narrative in plain English. The model's internal numbers (EV%, edge%, Kelly stake) are deliberately hidden — the UI is designed for a casual bettor, not a quant.

**Paper trading channel** (`DISCORD_WEBHOOK_PAPER`): Receives a "BET PLACED" embed for each simulated bet, then a P&L summary embed. At night after resolve runs, a results summary with CLV is sent here.

**Health channel** (`DISCORD_WEBHOOK_HEALTH`): Internal monitoring only. Receives: pipeline summary after each run (steps passed/failed, runtime, signal count by tier), immediate error alerts if any step fails, postponement alerts when a bet has been postponed 3+ times.

---

### The CLV Loop (Why It Matters)

Closing Line Value is the most important signal for whether the model is actually good or just getting lucky. Here's how it works:

When we place a bet, we capture the odds at that moment (e.g., -110 on over 5.5 Ks). When the game resolves, we also capture the closing odds — what the book was offering right before the game started (e.g., -130 on the same bet). Converting both to implied probabilities, CLV = closing_implied_prob - opening_implied_prob. In this example: -130 implied prob is ~56.5%, -110 is ~52.4%, so CLV = +4.1%.

Positive CLV means the market moved toward our position after we bet — we got a better price than the final market consensus. This is the primary signal that the model is finding real edges rather than noise. Over a full season, if avg CLV is consistently positive, that's strong evidence to move from paper to real money.

---

- No coding experience on the owner's side — always explain what you built and why in plain English after writing code
- Test every script immediately after writing it and fix errors before handing back
- Never commit `.env` — it contains real API keys
- Keep scrapers gentle on rate limits (2-second delays minimum)
- Save all raw data to `data/raw/` before any processing
- Prefer simple, readable code over clever one-liners
- After every session, update this file and push to GitHub
- **Running locally on Windows**: always prefix with `PYTHONIOENCODING=utf-8` — e.g. `PYTHONIOENCODING=utf-8 python main.py`. Without it, emoji/special characters in print statements cause a crash on Windows terminals (cp1252 encoding). Railway (Linux) is unaffected.

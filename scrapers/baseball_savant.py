"""
scrapers/baseball_savant.py

Pulls today's MLB probable starters and fetches their last-30-day
stats directly from Baseball Savant (Statcast) via pybaseball.

Stats per pitcher (30-day window):
  k_pct      : strikeout rate  (K / batters faced)
  xfip       : xFIP            (from FanGraphs if available, else estimated)
  velo       : avg fastball velocity (30-day)
  velo_trend : fastball velo last 7 days minus 30-day avg (+ = gaining, - = losing)
  spin_rate  : avg fastball spin rate (RPM)
  pitch_mix  : JSON dict of pitch type usage percentages
  babip      : batting average on balls in play

No API keys required.
"""

import os
import json
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from pybaseball import statcast_pitcher, pitching_stats_range
from pybaseball import cache

cache.enable()

OUTPUT_PATH    = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw', 'savant_today.csv')
HIST_STATS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'historical', 'pitcher_stats_all.csv')
DELAY = 2   # seconds between Statcast requests
FASTBALL_TYPES = {'FF', 'FT', 'SI', 'FC', 'FS'}  # pitch types treated as fastballs

END_DATE   = datetime.now()
START_DATE = END_DATE - timedelta(days=30)


# ---------------------------------------------------------------------------
# Step 1: Get today's probable starters from the free MLB Stats API
#         (statsapi.mlb.com is public — no key needed)
# ---------------------------------------------------------------------------

def get_todays_starters():
    today = datetime.now().strftime('%Y-%m-%d')
    url = (
        "https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1&date={today}&hydrate=probablePitcher(note)"
    )
    print(f"Fetching today's schedule ({today})...")
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    starters = []
    for date_block in data.get('dates', []):
        for game in date_block.get('games', []):
            for side in ('home', 'away'):
                team = game['teams'][side].get('team', {}).get('name', 'Unknown')
                pitcher = game['teams'][side].get('probablePitcher')
                if pitcher:
                    starters.append({
                        'name':   pitcher.get('fullName', ''),
                        'team':   team,
                        'mlb_id': pitcher.get('id'),
                        'throws': pitcher.get('pitchHand', {}).get('code', 'R'),
                    })

    print(f"  Found {len(starters)} probable starters.")
    return starters


# ---------------------------------------------------------------------------
# Step 2a: Fetch average innings per start from the MLB Stats API
#           Uses inningsPitched / gamesStarted for the current 2026 season.
#           Falls back to 2-year historical average, then 5.5 if no data.
# ---------------------------------------------------------------------------

def _parse_ip(ip_str) -> float:
    """
    Convert MLB Stats API innings string to true decimal innings.
    MLB uses X.Y where Y is outs (0/1/2), not tenths.
    '33.2' = 33 innings + 2 outs = 33.667 innings, not 33.2.
    """
    try:
        parts = str(ip_str).split('.')
        whole = int(parts[0])
        outs  = int(parts[1]) if len(parts) > 1 else 0
        return whole + outs / 3
    except Exception:
        return float(ip_str) if ip_str else 0.0


def _build_hist_ip_lookup() -> dict:
    """
    Build a name → avg IP/start dict from pitcher_stats_all.csv (2024+2025).
    Weighted average: 2025 counts 2x, 2024 counts 1x.
    Returns empty dict if file not found.
    """
    try:
        df = pd.read_csv(os.path.normpath(HIST_STATS_PATH))
        df = df[df['GS'] > 0].copy()
        df['ip_per_start'] = df['IP'] / df['GS']
        df['_name_lower']  = df['Name'].str.lower().str.strip()
        df['_weight']      = df['Season'].apply(lambda s: 2 if s == 2025 else 1)

        lookup = {}
        for name_lower, group in df.groupby('_name_lower'):
            total_w   = (group['ip_per_start'] * group['_weight']).sum()
            weight_sum = group['_weight'].sum()
            lookup[name_lower] = round(total_w / weight_sum, 2)
        return lookup
    except Exception:
        return {}


def _hist_ip_for(name: str, hist_lookup: dict) -> float | None:
    """Look up historical avg IP/start by name, trying last-name fallback."""
    name_lower = name.lower().strip()
    if name_lower in hist_lookup:
        return hist_lookup[name_lower]
    last = name_lower.split()[-1]
    for k, v in hist_lookup.items():
        if k.endswith(last):
            return v
    return None


def fetch_avg_ip(mlb_id, name, hist_lookup: dict) -> tuple[float, float | None]:
    """
    Returns (avg_ip, hist_avg_ip) for a pitcher.

    avg_ip      — current 2026 season IP/GS from MLB Stats API.
                  Falls back to hist_avg_ip, then 5.5 if API returns 0 starts.
    hist_avg_ip — weighted 2024+2025 average IP/start (None if not in history).

    Both values are returned so ev_calculator can blend them for early-season
    starts the same way it blends K/9.
    """
    DEFAULT_IP  = 5.5
    hist_ip     = _hist_ip_for(name, hist_lookup)
    current_ip  = None

    try:
        url  = (
            f"https://statsapi.mlb.com/api/v1/people/{mlb_id}"
            f"?hydrate=stats(group=pitching,type=season,season=2026)"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        for stat_group in data.get('people', [{}])[0].get('stats', []):
            splits = stat_group.get('splits', [])
            if not splits:
                continue
            stat = splits[0].get('stat', {})
            gs   = int(stat.get('gamesStarted', 0))
            if gs > 0:
                ip = _parse_ip(stat.get('inningsPitched', '0'))
                current_ip = round(ip / gs, 2)
                break
    except Exception as e:
        print(f"    avg_ip API error for {name}: {e}")

    # If no current starts yet, fall back to historical then default
    if current_ip is None:
        current_ip = hist_ip if hist_ip is not None else DEFAULT_IP

    return current_ip, hist_ip


# ---------------------------------------------------------------------------
# Step 2b: Try to get xFIP from FanGraphs for the season so far
#         (used to enrich the Statcast data; gracefully skipped if unavailable)
# ---------------------------------------------------------------------------

def get_fangraphs_xfip():
    start_str = START_DATE.strftime('%Y-%m-%d')
    end_str   = END_DATE.strftime('%Y-%m-%d')
    print(f"\nFetching xFIP from FanGraphs ({start_str} to {end_str})...")
    try:
        time.sleep(DELAY)
        df = pitching_stats_range(start_str, end_str)
        df['_name_lower'] = df['Name'].str.lower().str.strip()
        print(f"  FanGraphs returned stats for {len(df)} pitchers.")
        return df
    except Exception as e:
        print(f"  FanGraphs unavailable ({e}). xFIP will be shown as N/A.")
        return None


# ---------------------------------------------------------------------------
# Step 3: Pull pitch-by-pitch Statcast data for one pitcher
# ---------------------------------------------------------------------------

def fetch_pitcher_statcast(mlb_id, name):
    start_str = START_DATE.strftime('%Y-%m-%d')
    end_str   = END_DATE.strftime('%Y-%m-%d')
    try:
        time.sleep(DELAY)
        df = statcast_pitcher(start_str, end_str, player_id=mlb_id)
        return df
    except Exception as e:
        print(f"    Statcast error for {name}: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 4: Compute per-pitcher metrics from raw Statcast rows
# ---------------------------------------------------------------------------

def compute_metrics(df):
    metrics = {'k_pct': None, 'velo': None, 'velo_trend': None,
               'spin_rate': None, 'pitch_mix': None, 'babip': None}

    if df is None or df.empty:
        return metrics

    # --- Strikeout rate ---
    # A plate appearance ends on any 'events' value that isn't null
    pa_mask   = df['events'].notna()
    k_mask    = df['events'] == 'strikeout'
    pa_count  = pa_mask.sum()
    k_count   = k_mask.sum()
    if pa_count > 0:
        metrics['k_pct'] = round(k_count / pa_count, 3)

    # --- Average fastball velocity (30-day) ---
    fb_mask = df['pitch_type'].isin(FASTBALL_TYPES)
    fb_df   = df[fb_mask & df['release_speed'].notna()]
    if not fb_df.empty:
        metrics['velo'] = round(fb_df['release_speed'].mean(), 1)

    # --- Velocity trend: last 7 days vs 30-day average ---
    # Positive = pitcher throwing harder recently, negative = losing velo
    if not fb_df.empty and 'game_date' in fb_df.columns:
        cutoff = END_DATE - timedelta(days=7)
        recent = fb_df[pd.to_datetime(fb_df['game_date']) >= cutoff]
        if len(recent) >= 5:
            metrics['velo_trend'] = round(recent['release_speed'].mean() - fb_df['release_speed'].mean(), 1)

    # --- Fastball spin rate (RPM) ---
    if 'release_spin_rate' in df.columns:
        fb_spin = df[fb_mask & df['release_spin_rate'].notna()]
        if not fb_spin.empty:
            metrics['spin_rate'] = round(fb_spin['release_spin_rate'].mean(), 0)

    # --- Pitch mix: percentage of each pitch type thrown ---
    typed = df[df['pitch_type'].notna()]
    if not typed.empty:
        counts = typed['pitch_type'].value_counts()
        total  = counts.sum()
        metrics['pitch_mix'] = json.dumps({k: round(v / total, 3) for k, v in counts.items()})

    # --- BABIP ---
    # BABIP = (H - HR) / (AB - K - HR + SF)
    hit_events  = {'single', 'double', 'triple', 'home_run'}
    ab_events   = hit_events | {'strikeout', 'field_out', 'grounded_into_double_play',
                                 'double_play', 'fielders_choice', 'fielders_choice_out',
                                 'force_out', 'strikeout_double_play'}
    sf_events   = {'sac_fly'}

    events = df[df['events'].notna()]['events']
    hits   = events.isin(hit_events).sum()
    hrs    = (events == 'home_run').sum()
    abs_   = events.isin(ab_events).sum()
    ks     = (events == 'strikeout').sum()
    sfs    = events.isin(sf_events).sum()

    denom = abs_ - ks - hrs + sfs
    if denom > 0:
        metrics['babip'] = round((hits - hrs) / denom, 3)

    return metrics


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    print("=" * 55)
    print("  PLAYBOOK — Baseball Savant Scraper")
    print("=" * 55)

    # 1. Today's starters
    starters = get_todays_starters()
    if not starters:
        print("\nNo games scheduled today. Nothing to scrape.")
        return

    # 2. Historical IP lookup (for avg_ip fallback)
    hist_ip = _build_hist_ip_lookup()
    print(f"  Loaded historical IP averages for {len(hist_ip)} pitchers.")

    # 3. FanGraphs xFIP (best-effort)
    fg_df = get_fangraphs_xfip()

    def lookup_xfip(name):
        if fg_df is None:
            return None
        name_lower = name.lower().strip()
        match = fg_df[fg_df['_name_lower'] == name_lower]
        if match.empty:
            last = name_lower.split()[-1]
            match = fg_df[fg_df['_name_lower'].str.endswith(last)]
        if not match.empty:
            val = match.iloc[0].get('xFIP')
            return round(float(val), 2) if pd.notna(val) else None
        return None

    # 4. Per-pitcher Statcast stats + avg IP from MLB Stats API
    print(f"\nFetching Statcast data for {len(starters)} pitchers "
          f"({START_DATE.strftime('%Y-%m-%d')} to {END_DATE.strftime('%Y-%m-%d')})...")
    print(f"  (2-second delay between requests)\n")

    rows = []
    for i, p in enumerate(starters, 1):
        print(f"  [{i}/{len(starters)}] {p['name']} — {p['team']}")
        sc_data              = fetch_pitcher_statcast(p['mlb_id'], p['name'])
        metrics              = compute_metrics(sc_data)
        avg_ip, hist_avg_ip  = fetch_avg_ip(p['mlb_id'], p['name'], hist_ip)
        print(f"    avg_ip = {avg_ip} IP/start  (hist: {hist_avg_ip})")
        rows.append({
            'name':         p['name'],
            'team':         p['team'],
            'throws':       p.get('throws', 'R'),
            'k_pct':        metrics['k_pct'],
            'xfip':         lookup_xfip(p['name']),
            'velo':         metrics['velo'],
            'velo_trend':   metrics['velo_trend'],
            'spin_rate':    metrics['spin_rate'],
            'pitch_mix':    metrics['pitch_mix'],
            'babip':        metrics['babip'],
            'avg_ip':       avg_ip,
            'hist_avg_ip':  hist_avg_ip,
        })

    report = pd.DataFrame(rows)

    # 5. Save
    out_path = os.path.normpath(OUTPUT_PATH)
    report.to_csv(out_path, index=False)
    print(f"\nSaved to: {out_path}")

    # 6. Summary
    found = report['k_pct'].notna().sum()
    print(f"\n{'='*55}")
    print(f"  Summary")
    print(f"{'='*55}")
    print(f"  Pitchers today:     {len(report)}")
    print(f"  With Statcast data: {found}")
    print(f"  Missing data:       {len(report) - found}")

    print(f"\n--- Preview (first 10) ---")
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 120)
    print(report.head(10).to_string(index=False))


if __name__ == '__main__':
    run()

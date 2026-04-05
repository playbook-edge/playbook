"""
scrapers/baseball_savant.py

Pulls today's MLB probable starters and fetches their last-30-day
stats directly from Baseball Savant (Statcast) via pybaseball.

Stats per pitcher (30-day window):
  k_pct  : strikeout rate  (K / batters faced)
  xfip   : xFIP            (from FanGraphs if available, else estimated)
  velo   : avg fastball velocity
  babip  : batting average on balls in play

No API keys required.
"""

import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from pybaseball import statcast_pitcher, pitching_stats_range
from pybaseball import cache

cache.enable()

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw', 'savant_today.csv')
DELAY = 5   # seconds between Statcast requests
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
                    })

    print(f"  Found {len(starters)} probable starters.")
    return starters


# ---------------------------------------------------------------------------
# Step 2: Try to get xFIP from FanGraphs for the season so far
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
    metrics = {'k_pct': None, 'velo': None, 'babip': None}

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

    # --- Average fastball velocity ---
    fb_mask = df['pitch_type'].isin(FASTBALL_TYPES)
    fb_df   = df[fb_mask & df['release_speed'].notna()]
    if not fb_df.empty:
        metrics['velo'] = round(fb_df['release_speed'].mean(), 1)

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

    # 2. FanGraphs xFIP (best-effort)
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

    # 3. Per-pitcher Statcast stats
    print(f"\nFetching Statcast data for {len(starters)} pitchers "
          f"({START_DATE.strftime('%Y-%m-%d')} to {END_DATE.strftime('%Y-%m-%d')})...")
    print(f"  (5-second delay between requests)\n")

    rows = []
    for i, p in enumerate(starters, 1):
        print(f"  [{i}/{len(starters)}] {p['name']} — {p['team']}")
        sc_data = fetch_pitcher_statcast(p['mlb_id'], p['name'])
        metrics = compute_metrics(sc_data)
        rows.append({
            'name':  p['name'],
            'team':  p['team'],
            'k_pct': metrics['k_pct'],
            'xfip':  lookup_xfip(p['name']),
            'velo':  metrics['velo'],
            'babip': metrics['babip'],
        })

    report = pd.DataFrame(rows)

    # 4. Save
    out_path = os.path.normpath(OUTPUT_PATH)
    report.to_csv(out_path, index=False)
    print(f"\nSaved to: {out_path}")

    # 5. Summary
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

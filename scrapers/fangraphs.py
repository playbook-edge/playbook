"""
scrapers/fangraphs.py

Pulls two datasets from FanGraphs / Baseball Savant via pybaseball:

1. team_krates.csv  — Team strikeout rates split by batter handedness
                      (how much each team Ks vs RHP vs LHP)
2. pitcher_stats.csv — Current season pitching leaderboard:
                       xFIP, FIP, K/9, BB/9, BABIP for qualified starters

No API keys required.
"""

import os
import time
import pandas as pd
from datetime import datetime

from pybaseball import statcast, pitching_stats
from pybaseball import cache

cache.enable()

RAW_DIR        = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw')
TEAM_KRATES    = os.path.join(RAW_DIR, 'team_krates.csv')
PITCHER_STATS  = os.path.join(RAW_DIR, 'pitcher_stats.csv')

SEASON_START   = '2026-03-20'   # Opening Day 2026
TODAY          = datetime.now().strftime('%Y-%m-%d')
CURRENT_YEAR   = datetime.now().year
DELAY          = 5              # seconds between requests


# ---------------------------------------------------------------------------
# Helper: derive batting team from statcast columns
# Statcast has home_team / away_team + inning_topbot (Top = away batting)
# ---------------------------------------------------------------------------

def add_batting_team(df):
    df = df.copy()
    df['batting_team'] = df.apply(
        lambda r: r['away_team'] if r['inning_topbot'] == 'Top' else r['home_team'],
        axis=1
    )
    return df


# ---------------------------------------------------------------------------
# Part 1: Team K-rates by batter handedness
# ---------------------------------------------------------------------------

def build_team_krates():
    print(f"Fetching Statcast data ({SEASON_START} to {TODAY}) for team K-rate splits...")
    print("  (This may take 15-30 seconds)")
    time.sleep(DELAY)

    sc = statcast(SEASON_START, TODAY)
    sc = add_batting_team(sc)

    # Only rows where a plate appearance ended
    pa_df = sc[sc['events'].notna()].copy()

    results = []
    for hand in ('R', 'L'):
        label = 'vs_RHP' if hand == 'R' else 'vs_LHP'
        subset = pa_df[pa_df['p_throws'] == hand]  # pitcher handedness

        team_stats = (
            subset
            .groupby('batting_team')
            .agg(
                pa=('events', 'count'),
                k=('events', lambda x: (x == 'strikeout').sum())
            )
            .reset_index()
        )
        team_stats['k_pct']    = (team_stats['k'] / team_stats['pa']).round(3)
        team_stats['matchup']  = label
        team_stats['hand']     = hand
        results.append(team_stats)

    combined = pd.concat(results, ignore_index=True)

    # Pivot to wide format: one row per team, columns for vs_RHP and vs_LHP
    wide = combined.pivot(index='batting_team', columns='matchup', values='k_pct').reset_index()
    wide.columns.name = None
    wide = wide.rename(columns={'batting_team': 'team'})

    # Add raw PA counts for context
    pa_rhp = combined[combined['hand'] == 'R'][['batting_team', 'pa']].rename(
        columns={'batting_team': 'team', 'pa': 'pa_vs_rhp'})
    pa_lhp = combined[combined['hand'] == 'L'][['batting_team', 'pa']].rename(
        columns={'batting_team': 'team', 'pa': 'pa_vs_lhp'})

    wide = wide.merge(pa_rhp, on='team', how='left')
    wide = wide.merge(pa_lhp, on='team', how='left')
    wide = wide.sort_values('vs_RHP', ascending=False).reset_index(drop=True)

    return wide


# ---------------------------------------------------------------------------
# Part 2: Current season pitching leaderboard
# ---------------------------------------------------------------------------

def build_pitcher_leaderboard():
    print(f"\nFetching {CURRENT_YEAR} season pitching leaderboard from FanGraphs...")
    time.sleep(DELAY)

    # qual=0 includes all pitchers; we'll filter to starters with enough IP
    df = pitching_stats(CURRENT_YEAR, CURRENT_YEAR, qual=0)

    # Keep only starting pitchers with at least 5 IP
    # 'Start-IP' = innings as a starter; 'G' = games, 'GS' = games started
    starters = df[
        (df.get('GS', df.get('G', 0)) > 0) &
        (df['IP'] >= 5)
    ].copy()

    # Select and rename columns cleanly
    cols = {
        'Name':  'name',
        'Team':  'team',
        'IP':    'ip',
        'K/9':   'k9',
        'BB/9':  'bb9',
        'FIP':   'fip',
        'xFIP':  'xfip',
        'BABIP': 'babip',
        'ERA':   'era',
        'GS':    'starts',
    }
    available = {k: v for k, v in cols.items() if k in starters.columns}
    leaderboard = starters[list(available.keys())].rename(columns=available)
    leaderboard = leaderboard.sort_values('xfip').reset_index(drop=True)

    return leaderboard


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    print("=" * 55)
    print("  PLAYBOOK -- FanGraphs Scraper")
    print("=" * 55)

    os.makedirs(RAW_DIR, exist_ok=True)

    # --- Part 1: Team K-rates ---
    krates = build_team_krates()
    krates.to_csv(TEAM_KRATES, index=False)
    print(f"\nSaved to: {os.path.normpath(TEAM_KRATES)}")

    print(f"\n--- Team K-Rates by Batter Handedness (top 10 vs RHP) ---")
    print(f"{'Team':<28} {'vs RHP':>7} {'PA':>6} {'vs LHP':>7} {'PA':>6}")
    print("-" * 58)
    for _, r in krates.head(10).iterrows():
        rhp_pct = f"{r['vs_RHP']:.1%}" if pd.notna(r.get('vs_RHP')) else 'N/A'
        lhp_pct = f"{r['vs_LHP']:.1%}" if pd.notna(r.get('vs_LHP')) else 'N/A'
        pa_rhp  = int(r['pa_vs_rhp']) if pd.notna(r.get('pa_vs_rhp')) else 0
        pa_lhp  = int(r['pa_vs_lhp']) if pd.notna(r.get('pa_vs_lhp')) else 0
        print(f"  {r['team']:<26} {rhp_pct:>7} {pa_rhp:>6} {lhp_pct:>7} {pa_lhp:>6}")

    # --- Part 2: Pitcher Leaderboard ---
    leaderboard = build_pitcher_leaderboard()
    leaderboard.to_csv(PITCHER_STATS, index=False)
    print(f"\nSaved to: {os.path.normpath(PITCHER_STATS)}")

    print(f"\n--- Pitching Leaderboard: Best xFIP (top 15) ---")
    print(f"{'Name':<22} {'Team':<5} {'IP':>5} {'GS':>4} {'ERA':>5} {'FIP':>5} {'xFIP':>5} {'K/9':>5} {'BB/9':>5} {'BABIP':>6}")
    print("-" * 72)
    for _, r in leaderboard.head(15).iterrows():
        print(
            f"  {str(r.get('name','')):<20} "
            f"{str(r.get('team','')):<5} "
            f"{r.get('ip', 0):>5.1f} "
            f"{int(r.get('starts', 0)):>4} "
            f"{r.get('era', 0):>5.2f} "
            f"{r.get('fip', 0):>5.2f} "
            f"{r.get('xfip', 0):>5.2f} "
            f"{r.get('k9', 0):>5.1f} "
            f"{r.get('bb9', 0):>5.1f} "
            f"{r.get('babip', 0):>6.3f}"
        )

    print(f"\n--- Summary ---")
    print(f"  Teams with K-rate splits: {len(krates)}")
    print(f"  Pitchers on leaderboard:  {len(leaderboard)}")
    print(f"  Season through:           {TODAY}")


if __name__ == '__main__':
    run()

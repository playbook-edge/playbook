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
import sys
import time
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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
# Part 1: Team K-rates by batter handedness  (incremental Statcast pulls)
#
# How it works:
#   - First ever run:   full pull from SEASON_START, stores running K/PA
#     counts in team_krates_cache and records TODAY in statcast_pull_log.
#   - Same-day re-run:  fetch_date == TODAY in team_krates_cache toinstant
#     return, zero API calls.
#   - Next day's run:   incremental pull from last_pull_date+1 to TODAY,
#     adds new K/PA counts to the stored totals, recalculates K-rates,
#     updates both tables.
#
# team_krates_cache stores k_vs_rhp / k_vs_lhp (raw K counts) in addition
# to the aggregated K-rates so daily merging can be done without re-pulling
# historical data.
# ---------------------------------------------------------------------------

def _aggregate_krates(sc_df: pd.DataFrame) -> dict:
    """
    Given a raw Statcast DataFrame, return per-team K/PA counts by pitcher hand.
    Returns: { team: {'pa_rhp', 'k_rhp', 'pa_lhp', 'k_lhp'} }
    """
    sc_df  = add_batting_team(sc_df)
    pa_df  = sc_df[sc_df['events'].notna()].copy()
    totals = {}

    for hand in ('R', 'L'):
        pa_key = 'pa_rhp' if hand == 'R' else 'pa_lhp'
        k_key  = 'k_rhp'  if hand == 'R' else 'k_lhp'
        subset = pa_df[pa_df['p_throws'] == hand]
        stats  = (
            subset
            .groupby('batting_team')
            .agg(pa=('events', 'count'),
                 k=('events', lambda x: (x == 'strikeout').sum()))
            .reset_index()
        )
        for _, row in stats.iterrows():
            team = row['batting_team']
            if team not in totals:
                totals[team] = {'pa_rhp': 0, 'k_rhp': 0, 'pa_lhp': 0, 'k_lhp': 0}
            totals[team][pa_key] = int(row['pa'])
            totals[team][k_key]  = int(row['k'])

    return totals


def _totals_to_wide(totals: dict) -> pd.DataFrame:
    """Convert {team: {pa_rhp, k_rhp, pa_lhp, k_lhp}} to the wide K-rate DataFrame."""
    rows = []
    for team, t in totals.items():
        vs_rhp = round(t['k_rhp'] / t['pa_rhp'], 3) if t['pa_rhp'] > 0 else None
        vs_lhp = round(t['k_lhp'] / t['pa_lhp'], 3) if t['pa_lhp'] > 0 else None
        rows.append({
            'team':      team,
            'vs_RHP':    vs_rhp,
            'vs_LHP':    vs_lhp,
            'pa_vs_rhp': t['pa_rhp'],
            'pa_vs_lhp': t['pa_lhp'],
            'k_vs_rhp':  t['k_rhp'],
            'k_vs_lhp':  t['k_lhp'],
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values('vs_RHP', ascending=False).reset_index(drop=True)


def _save_krates_to_supabase(wide: pd.DataFrame, total_rows: int):
    """Overwrite team_krates_cache and update statcast_pull_log."""
    try:
        from database import get_client
        client = get_client()
        if not client:
            return

        # Overwrite K-rates cache
        client.table('team_krates_cache').delete().neq('id', 0).execute()
        records = []
        for _, r in wide.iterrows():
            records.append({
                'fetch_date': TODAY,
                'team':       str(r['team']),
                'vs_rhp':     float(r['vs_RHP'])    if pd.notna(r.get('vs_RHP'))    else None,
                'vs_lhp':     float(r['vs_LHP'])    if pd.notna(r.get('vs_LHP'))    else None,
                'pa_vs_rhp':  int(r['pa_vs_rhp'])   if pd.notna(r.get('pa_vs_rhp')) else None,
                'pa_vs_lhp':  int(r['pa_vs_lhp'])   if pd.notna(r.get('pa_vs_lhp')) else None,
                'k_vs_rhp':   int(r['k_vs_rhp'])    if pd.notna(r.get('k_vs_rhp'))  else None,
                'k_vs_lhp':   int(r['k_vs_lhp'])    if pd.notna(r.get('k_vs_lhp'))  else None,
            })
        client.table('team_krates_cache').insert(records).execute()

        # Update pull log (keep only one row)
        client.table('statcast_pull_log').delete().neq('id', 0).execute()
        client.table('statcast_pull_log').insert({
            'last_pull_date':    TODAY,
            'total_rows_cached': total_rows,
        }).execute()

        print(f'  Supabase updated — {len(records)} teams, {total_rows:,} total rows logged.')
    except Exception as e:
        print(f'  Supabase cache write failed ({e}) — continuing with in-memory result.')


def build_team_krates():
    """
    Build team K-rate splits using incremental Statcast pulls.
    See module docstring above for the three execution paths.
    """
    t_start = time.time()

    # ── Path 1: already ran today — instant return ───────────────────────────
    try:
        from database import get_client
        client = get_client()
        if client:
            resp = client.table('team_krates_cache').select('*').eq('fetch_date', TODAY).execute()
            if resp.data:
                elapsed = round(time.time() - t_start, 1)
                print(f'  Using cached team K-rates from today ({len(resp.data)} teams) [{elapsed}s]')
                df = pd.DataFrame(resp.data).drop(
                    columns=['id', 'fetch_date', 'k_vs_rhp', 'k_vs_lhp'], errors='ignore')
                df = df.rename(columns={'vs_rhp': 'vs_RHP', 'vs_lhp': 'vs_LHP'})
                return df.sort_values('vs_RHP', ascending=False).reset_index(drop=True)
    except Exception as e:
        print(f'  Cache check failed ({e}) — proceeding with pull.')

    # ── Determine last pull date and load existing totals ────────────────────
    last_pull_date    = None
    existing_totals   = {}
    total_rows_so_far = 0
    can_increment     = False

    try:
        from database import get_client
        client = get_client()
        if client:
            log_resp = (client.table('statcast_pull_log')
                              .select('*')
                              .order('id', desc=True)
                              .limit(1)
                              .execute())
            if log_resp.data:
                last_pull_date    = log_resp.data[0]['last_pull_date']
                total_rows_so_far = int(log_resp.data[0].get('total_rows_cached', 0))

                # Load existing running totals (needs k_vs_rhp/k_vs_lhp columns)
                cache_resp = client.table('team_krates_cache').select('*').execute()
                if cache_resp.data and cache_resp.data[0].get('k_vs_rhp') is not None:
                    for row in cache_resp.data:
                        existing_totals[row['team']] = {
                            'pa_rhp': int(row.get('pa_vs_rhp') or 0),
                            'k_rhp':  int(row.get('k_vs_rhp')  or 0),
                            'pa_lhp': int(row.get('pa_vs_lhp') or 0),
                            'k_lhp':  int(row.get('k_vs_lhp')  or 0),
                        }
                    can_increment = True
    except Exception as e:
        print(f'  Pull log check failed ({e}) — doing full pull.')

    # ── Path 2: incremental pull ─────────────────────────────────────────────
    if can_increment and last_pull_date and last_pull_date < TODAY:
        from datetime import datetime as _dt, timedelta
        pull_from = (_dt.strptime(str(last_pull_date), '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        print(f'  Incremental pull: {pull_from} to{TODAY}  '
              f'(baseline: {total_rows_so_far:,} rows)')
        time.sleep(DELAY)

        sc_new = statcast(pull_from, TODAY)
        new_row_count = len(sc_new)
        print(f'  Fetched {new_row_count:,} new rows.')

        if sc_new.empty or new_row_count == 0:
            # Off-day — no new data, return existing cache as-is
            print('  No new games found. Returning existing K-rates.')
            wide = _totals_to_wide(existing_totals)
            _save_krates_to_supabase(wide, total_rows_so_far)
            elapsed = round(time.time() - t_start, 1)
            print(f'  Done [{elapsed}s]')
            return wide

        new_totals = _aggregate_krates(sc_new)

        # Merge: add new counts onto existing running totals
        all_teams = set(existing_totals) | set(new_totals)
        merged = {}
        for team in all_teams:
            ex = existing_totals.get(team, {'pa_rhp': 0, 'k_rhp': 0, 'pa_lhp': 0, 'k_lhp': 0})
            nw = new_totals.get(team,      {'pa_rhp': 0, 'k_rhp': 0, 'pa_lhp': 0, 'k_lhp': 0})
            merged[team] = {
                'pa_rhp': ex['pa_rhp'] + nw['pa_rhp'],
                'k_rhp':  ex['k_rhp']  + nw['k_rhp'],
                'pa_lhp': ex['pa_lhp'] + nw['pa_lhp'],
                'k_lhp':  ex['k_lhp']  + nw['k_lhp'],
            }

        wide = _totals_to_wide(merged)
        _save_krates_to_supabase(wide, total_rows_so_far + new_row_count)
        elapsed = round(time.time() - t_start, 1)
        print(f'  Incremental update complete [{elapsed}s]')
        return wide

    # ── Path 3: full pull — first run or missing baseline ────────────────────
    print(f'  Full pull: {SEASON_START} to{TODAY}  (establishing baseline)')
    print('  (This takes 15-60 seconds; subsequent runs will be incremental)')
    time.sleep(DELAY)

    sc_full = statcast(SEASON_START, TODAY)
    full_row_count = len(sc_full)
    print(f'  Fetched {full_row_count:,} rows.')

    totals = _aggregate_krates(sc_full)
    wide   = _totals_to_wide(totals)
    _save_krates_to_supabase(wide, full_row_count)
    elapsed = round(time.time() - t_start, 1)
    print(f'  Full pull complete [{elapsed}s]')
    return wide


# ---------------------------------------------------------------------------
# Part 2: Current season pitching leaderboard
# ---------------------------------------------------------------------------

def _parse_leaderboard(df: pd.DataFrame) -> pd.DataFrame:
    """Filter and rename a raw pitching stats DataFrame into leaderboard format."""
    starters = df[
        (df.get('GS', df.get('G', pd.Series(dtype=float))) > 0) &
        (df['IP'] >= 5)
    ].copy()
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
    return leaderboard.sort_values('xfip').reset_index(drop=True)


def build_pitcher_leaderboard():
    print(f"\nFetching {CURRENT_YEAR} season pitching leaderboard from FanGraphs...")

    # ── Same-day cache: if pitcher_stats.csv was written today, skip the fetch ──
    if os.path.exists(PITCHER_STATS):
        mtime = datetime.fromtimestamp(os.path.getmtime(PITCHER_STATS)).strftime('%Y-%m-%d')
        if mtime == TODAY:
            cached = pd.read_csv(PITCHER_STATS)
            print(f'  Using cached pitcher_stats.csv from today ({len(cached)} pitchers) [0s]')
            return cached

    # ── Try pybaseball → FanGraphs ───────────────────────────────────────────
    # FanGraphs blocks server IPs (Railway, etc.) with 403.  We attempt the
    # fetch and fall back to yesterday's file rather than crashing the pipeline.
    time.sleep(DELAY)
    try:
        raw = pitching_stats(CURRENT_YEAR, CURRENT_YEAR, qual=0)
        leaderboard = _parse_leaderboard(raw)
        print(f'  FanGraphs fetch successful ({len(leaderboard)} starters)')
        return leaderboard
    except Exception as e:
        print(f'  WARNING: FanGraphs fetch failed ({e})')

    # ── Fallback: use yesterday's cached file ────────────────────────────────
    # Pitcher K/9 and xFIP change very slowly; one stale day is fine.
    # ev_calculator already falls back to hist_xfip when xFIP is None, so
    # even a fully missing leaderboard degrades gracefully.
    if os.path.exists(PITCHER_STATS):
        cached = pd.read_csv(PITCHER_STATS)
        print(f'  Using previous pitcher_stats.csv ({len(cached)} pitchers) — xFIP fallback active')
        return cached

    print('  No pitcher leaderboard available — ev_calculator will use hist_xfip fallback')
    return pd.DataFrame()


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
    if not leaderboard.empty:
        leaderboard.to_csv(PITCHER_STATS, index=False)
        print(f"\nSaved to: {os.path.normpath(PITCHER_STATS)}")
    else:
        print(f"\nWARNING: Skipping pitcher_stats.csv write — no data returned (preserving any existing cache)")

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

"""
scrapers/historical_stats.py

Pulls full-season FanGraphs pitching stats for 2024 and 2025.
Saves each year to data/historical/ for use by the baseline model.

Run once to seed the historical database, then re-run at season end
to add the new year.
"""

import os
import sys
import time
import requests
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pybaseball
from pybaseball import pitching_stats
from pybaseball import cache

cache.enable()

# ── Patch pybaseball's requests calls to use browser headers ──────────────
_BROWSER_HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Referer':         'https://www.fangraphs.com/',
}
_original_requests_get = requests.get
def _patched_get(url, **kwargs):
    if 'fangraphs.com' in str(url):
        hdrs = dict(_BROWSER_HEADERS)
        hdrs.update(kwargs.pop('headers', {}))
        kwargs['headers'] = hdrs
    return _original_requests_get(url, **kwargs)
requests.get = _patched_get

HIST_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'historical')
DELAY    = 5

SEASONS  = [2024, 2025]

# Columns we care about — everything needed for baselines
KEEP_COLS = [
    'Name', 'Team', 'Season', 'Age',
    'G', 'GS', 'IP',
    'K/9', 'BB/9', 'K%', 'BB%',
    'ERA', 'FIP', 'xFIP', 'BABIP',
    'LOB%', 'HR/9',
]


def pull_season(year: int) -> pd.DataFrame:
    print(f'  Fetching {year} season from FanGraphs...')
    df = None
    for attempt in (1, 2):
        time.sleep(DELAY if attempt == 1 else 15)
        try:
            df = pitching_stats(year, year, qual=0)
            status = 'OK'
            print(f'  FanGraphs fetch successful (attempt {attempt})')
            break
        except Exception as e:
            status = getattr(getattr(e, 'response', None), 'status_code', 'N/A')
            print(f'  WARNING: FanGraphs attempt {attempt} failed — HTTP {status} ({e})')
            if attempt == 1:
                print('  Retrying in 15 seconds...')
    if df is None:
        raise RuntimeError(f'FanGraphs fetch failed for {year} after 2 attempts')

    # Add season column
    df['Season'] = year

    # Keep only columns that exist in this year's data
    cols = [c for c in KEEP_COLS if c in df.columns]
    df   = df[cols].copy()

    # Only keep pitchers with meaningful innings (5+ IP as a starter)
    if 'IP' in df.columns:
        df = df[df['IP'] >= 5].reset_index(drop=True)

    print(f'    {len(df)} pitchers with 5+ IP')
    return df


def run():
    print('=' * 55)
    print('  PLAYBOOK -- Historical Stats Scraper')
    print('=' * 55)

    os.makedirs(HIST_DIR, exist_ok=True)

    all_seasons = []

    for year in SEASONS:
        out_path = os.path.join(HIST_DIR, f'pitcher_stats_{year}.csv')

        if os.path.exists(out_path):
            print(f'  {year}: already cached at {os.path.basename(out_path)} — skipping')
            df = pd.read_csv(out_path)
        else:
            df = pull_season(year)
            df.to_csv(out_path, index=False)
            print(f'    Saved to {os.path.basename(out_path)}')

        all_seasons.append(df)

    combined = pd.concat(all_seasons, ignore_index=True)
    combined_path = os.path.join(HIST_DIR, 'pitcher_stats_all.csv')
    combined.to_csv(combined_path, index=False)

    print(f'\nCombined file: {len(combined)} pitcher-seasons')
    print(f'Saved to: {os.path.normpath(combined_path)}')

    print('\n--- Sample (sorted by xFIP, 2025) ---')
    sample = combined[combined['Season'] == 2025].sort_values('xFIP').head(10)
    print(sample[['Name', 'Team', 'IP', 'K/9', 'xFIP', 'BABIP']].to_string(index=False))


if __name__ == '__main__':
    run()

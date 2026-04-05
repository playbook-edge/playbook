"""
models/player_baseline.py

Builds per-pitcher historical baselines from 2024-2025 season data.

For each pitcher we compute:
  - 2-year weighted averages (recent year weighted 2x)
  - Standard deviation across seasons (consistency)
  - A Reliability Score (0-100): how much to trust their current stats
  - Trend direction vs historical avg (UP / DOWN / STABLE)

Output: data/historical/player_baselines.csv

Used by ev_calculator.py to weight PlaybookIQ with historical context.
"""

import os
import sys
import numpy as np
import pandas as pd
from difflib import SequenceMatcher

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

HIST_DIR  = os.path.join(os.path.dirname(__file__), '..', 'data', 'historical')
RAW_DIR   = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw')
OUT_PATH  = os.path.join(HIST_DIR, 'player_baselines.csv')

# Weight recent season more heavily
SEASON_WEIGHTS = {2024: 1.0, 2025: 2.0}

# Thresholds for trend arrows
TREND_UP_THRESHOLD   =  0.08    # current > hist by 8%+ relative
TREND_DOWN_THRESHOLD = -0.08


def normalize_name(name: str) -> str:
    if not isinstance(name, str):
        return ''
    name = name.lower().strip()
    for suffix in [' jr.', ' sr.', ' jr', ' sr', ' iii', ' ii', ' iv']:
        name = name.replace(suffix, '')
    return name.strip()


def fuzzy_match(target: str, candidates: list[str], threshold: float = 0.82) -> str | None:
    """Find the best matching name above the similarity threshold."""
    norm_target = normalize_name(target)
    best_score  = 0
    best_match  = None
    for c in candidates:
        score = SequenceMatcher(None, norm_target, normalize_name(c)).ratio()
        if score > best_score:
            best_score = score
            best_match = c
    return best_match if best_score >= threshold else None


def weighted_avg(values: list, weights: list) -> float | None:
    """Weighted average, ignoring NaN values."""
    pairs = [(v, w) for v, w in zip(values, weights) if pd.notna(v)]
    if not pairs:
        return None
    total_w = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total_w


def reliability_score(row: dict) -> int:
    """
    0-100 score reflecting how much to trust a pitcher's historical stats.

    Components:
      Seasons of data    0-25 pts  (2 seasons = 25, 1 season = 12)
      Total IP           0-35 pts  (200+ IP = 35)
      K/9 consistency   0-25 pts  (low std = consistent = more reliable)
      xFIP consistency   0-15 pts  (low std = more reliable)
    """
    seasons = row.get('seasons_in_data', 0)
    season_pts = 25 if seasons >= 2 else (12 if seasons == 1 else 0)

    total_ip = row.get('total_ip', 0) or 0
    ip_pts   = min(total_ip / 200, 1.0) * 35

    k9_std = row.get('k9_std')
    if k9_std is not None and pd.notna(k9_std):
        k9_pts = max(0, (1 - k9_std / 3.0)) * 25
    else:
        k9_pts = 12.5   # neutral

    xfip_std = row.get('xfip_std')
    if xfip_std is not None and pd.notna(xfip_std):
        xfip_pts = max(0, (1 - xfip_std / 1.5)) * 15
    else:
        xfip_pts = 7.5  # neutral

    total = season_pts + ip_pts + k9_pts + xfip_pts
    return int(round(min(max(total, 0), 100)))


def trend_label(current: float | None, hist_avg: float | None,
                higher_is_better: bool = True) -> str:
    """UP / DOWN / STABLE compared to historical average."""
    if current is None or hist_avg is None or hist_avg == 0:
        return 'NEW'
    if pd.isna(current) or pd.isna(hist_avg):
        return 'NEW'

    rel_change = (current - hist_avg) / abs(hist_avg)

    if higher_is_better:
        if rel_change >= TREND_UP_THRESHOLD:
            return 'UP'
        if rel_change <= TREND_DOWN_THRESHOLD:
            return 'DOWN'
    else:
        # For xFIP and BABIP: lower is better, so flip the direction
        if rel_change <= -TREND_UP_THRESHOLD:
            return 'UP'      # getting better
        if rel_change >= -TREND_DOWN_THRESHOLD:
            return 'DOWN'    # getting worse
    return 'STABLE'


def build_baselines(hist_df: pd.DataFrame,
                    current_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Compute per-pitcher baselines from historical data.
    Optionally enrich with current-season stats to generate trend arrows.
    """
    rows = []
    names = hist_df['Name'].dropna().unique()

    for name in names:
        pitcher_rows = hist_df[hist_df['Name'] == name].sort_values('Season')
        seasons = sorted(pitcher_rows['Season'].tolist())
        weights = [SEASON_WEIGHTS.get(s, 1.0) for s in seasons]

        def col_vals(col):
            return pitcher_rows[col].tolist() if col in pitcher_rows.columns else []

        k9_vals    = col_vals('K/9')
        xfip_vals  = col_vals('xFIP')
        babip_vals = col_vals('BABIP')
        bb9_vals   = col_vals('BB/9')
        ip_vals    = col_vals('IP')

        hist_k9    = weighted_avg(k9_vals, weights)
        hist_xfip  = weighted_avg(xfip_vals, weights)
        hist_babip = weighted_avg(babip_vals, weights)
        hist_bb9   = weighted_avg(bb9_vals, weights)
        total_ip   = sum(v for v in ip_vals if pd.notna(v))

        k9_std    = float(np.std(k9_vals))    if len(k9_vals) > 1    else None
        xfip_std  = float(np.std(xfip_vals))  if len(xfip_vals) > 1  else None

        row = {
            'name':           name,
            'seasons_in_data': len(seasons),
            'seasons':        ','.join(str(s) for s in seasons),
            'total_ip':       round(total_ip, 1),
            'hist_k9':        round(hist_k9, 2)    if hist_k9    is not None else None,
            'hist_xfip':      round(hist_xfip, 2)  if hist_xfip  is not None else None,
            'hist_babip':     round(hist_babip, 3) if hist_babip  is not None else None,
            'hist_bb9':       round(hist_bb9, 2)   if hist_bb9    is not None else None,
            'k9_std':         round(k9_std, 2)     if k9_std      is not None else None,
            'xfip_std':       round(xfip_std, 2)   if xfip_std    is not None else None,
        }

        row['reliability'] = reliability_score(row)

        # --- Trend vs current season ---
        if current_df is not None:
            norm_names  = current_df['name'].apply(normalize_name).tolist()
            matched     = fuzzy_match(name, current_df['name'].tolist())
            if matched:
                curr = current_df[current_df['name'] == matched].iloc[0]
                curr_k9   = curr.get('k9')
                curr_xfip = curr.get('xfip')
                curr_babip = curr.get('babip')

                row['curr_k9']    = round(float(curr_k9), 2)   if pd.notna(curr_k9)    else None
                row['curr_xfip']  = round(float(curr_xfip), 2) if pd.notna(curr_xfip)  else None
                row['curr_babip'] = round(float(curr_babip), 3) if pd.notna(curr_babip) else None

                row['k9_trend']    = trend_label(row.get('curr_k9'),    hist_k9,    higher_is_better=True)
                row['xfip_trend']  = trend_label(row.get('curr_xfip'),  hist_xfip,  higher_is_better=False)
                row['babip_trend'] = trend_label(row.get('curr_babip'), hist_babip, higher_is_better=False)
            else:
                row['curr_k9']     = None
                row['curr_xfip']   = None
                row['curr_babip']  = None
                row['k9_trend']    = 'NEW'
                row['xfip_trend']  = 'NEW'
                row['babip_trend'] = 'NEW'

        rows.append(row)

    return pd.DataFrame(rows).sort_values('reliability', ascending=False).reset_index(drop=True)


def lookup_baseline(pitcher_name: str, baselines_df: pd.DataFrame) -> dict | None:
    """
    Return a pitcher's baseline row as a dict.
    Used by ev_calculator and discord_alerts.
    Returns None if not found.
    """
    names   = baselines_df['name'].tolist()
    matched = fuzzy_match(pitcher_name, names)
    if matched:
        return baselines_df[baselines_df['name'] == matched].iloc[0].to_dict()
    return None


def run():
    print('=' * 55)
    print('  PLAYBOOK -- Player Baseline Builder')
    print('=' * 55)

    all_path = os.path.join(HIST_DIR, 'pitcher_stats_all.csv')
    if not os.path.exists(all_path):
        print('No historical data found.')
        print('Run scrapers/historical_stats.py first.')
        return

    hist_df = pd.read_csv(all_path)
    print(f'Loaded {len(hist_df)} pitcher-seasons ({hist_df["Season"].min():.0f}-{hist_df["Season"].max():.0f})')

    # Load current season for trend arrows
    current_path = os.path.join(RAW_DIR, 'pitcher_stats.csv')
    current_df   = pd.read_csv(current_path) if os.path.exists(current_path) else None
    if current_df is not None:
        print(f'Loaded {len(current_df)} current-season pitchers for trend comparison')

    print('Building baselines...')
    baselines = build_baselines(hist_df, current_df)
    baselines.to_csv(OUT_PATH, index=False)

    print(f'Saved {len(baselines)} player baselines to: {os.path.normpath(OUT_PATH)}')

    # ── Summary stats ──
    has_history  = baselines[baselines['seasons_in_data'] >= 2]
    one_season   = baselines[baselines['seasons_in_data'] == 1]

    print(f'\n--- Coverage ---')
    print(f'  2 seasons of data:  {len(has_history)} pitchers')
    print(f'  1 season of data:   {len(one_season)} pitchers')

    # ── Top 10 most reliable with trend data ──
    with_trends = baselines[baselines.get('k9_trend', pd.Series(dtype=str)).notna()
                            if 'k9_trend' in baselines.columns else [True]*len(baselines)]

    print(f'\n--- Top 10 Most Reliable (2yr history, current-season trends) ---')
    cols = ['name', 'total_ip', 'hist_k9', 'hist_xfip', 'reliability']
    if 'curr_k9' in baselines.columns:
        cols += ['curr_k9', 'k9_trend', 'xfip_trend']
    print(baselines[cols].head(10).to_string(index=False))

    # ── Biggest positive K/9 movers ──
    if 'curr_k9' in baselines.columns and 'hist_k9' in baselines.columns:
        movers = baselines.dropna(subset=['curr_k9', 'hist_k9']).copy()
        movers['k9_delta'] = movers['curr_k9'] - movers['hist_k9']

        print(f'\n--- Biggest K/9 Improvers vs History ---')
        top_up = movers.sort_values('k9_delta', ascending=False).head(8)
        for _, r in top_up.iterrows():
            print(f"  {r['name']:<24}  hist: {r['hist_k9']:.1f}  curr: {r['curr_k9']:.1f}  delta: {r['k9_delta']:+.1f}  reliability: {r['reliability']}")

        print(f'\n--- Biggest K/9 Decliners vs History ---')
        top_dn = movers.sort_values('k9_delta').head(8)
        for _, r in top_dn.iterrows():
            print(f"  {r['name']:<24}  hist: {r['hist_k9']:.1f}  curr: {r['curr_k9']:.1f}  delta: {r['k9_delta']:+.1f}  reliability: {r['reliability']}")


if __name__ == '__main__':
    run()

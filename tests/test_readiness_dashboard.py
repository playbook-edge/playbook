"""
tests/test_readiness_dashboard.py

Injects synthetic resolved trades + CLV records, runs the readiness
dashboard in preview mode, then cleans up every record it created.

Run: python tests/test_readiness_dashboard.py
"""

import os
import sys
import random
import csv
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

TRADES_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'processed', 'paper_trades.csv')
TRADE_COLUMNS = [
    'date', 'player', 'prop_type', 'side', 'line', 'odds',
    'ev', 'stake', 'bankroll_before', 'bankroll_after',
    'result', 'payout', 'net', 'matchup', 'book', 'postpone_count'
]
TEST_TAG = '__TEST__'


# ─────────────────────────────────────────────
# Synthetic data definitions
# ─────────────────────────────────────────────

# Realistic mix: 45 resolved bets spread across tiers and outcomes.
# Designed to produce a MONITOR verdict:
#   - Positive avg CLV (model is finding real edges)
#   - Slightly negative ROI (variance running against us early)
#   - 44% win rate vs ~49% break-even (expected for -110 juice)

SYNTHETIC_TRADES = [
    # Conservative (EV 4-7%) — 15 bets, 8W 7L, solid CLV
    ('Garrett Crochet',    'Over',  6.5, -115, 0.068,  20.00, 'WIN',  17.39),
    ('Tarik Skubal',       'Over',  5.5, -110, 0.055,  30.00, 'WIN',  27.27),
    ('Logan Gilbert',      'Under', 5.5, -105, 0.062,  20.00, 'LOSS', 0.00),
    ('Cole Ragans',        'Over',  5.5, +100, 0.051,  30.00, 'WIN',  30.00),
    ('Aaron Nola',         'Under', 6.5, -110, 0.058,  20.00, 'WIN',  18.18),
    ('Framber Valdez',     'Over',  4.5, -115, 0.064,  20.00, 'LOSS', 0.00),
    ('Max Fried',          'Over',  4.5, -110, 0.059,  20.00, 'WIN',  18.18),
    ('Dylan Cease',        'Under', 7.5, -105, 0.047,  30.00, 'LOSS', 0.00),
    ('Luis Castillo',      'Under', 6.0, -130, 0.053,  20.00, 'WIN',  15.38),
    ('George Kirby',       'Over',  5.5, -105, 0.066,  20.00, 'LOSS', 0.00),
    ('Jose Soriano',       'Under', 5.0, +105, 0.049,  20.00, 'WIN',  21.00),
    ('Joe Ryan',           'Over',  4.0, -110, 0.058,  20.00, 'LOSS', 0.00),
    ('Andrew Abbott',      'Over',  4.0, +105, 0.052,  20.00, 'WIN',  21.00),
    ('Tanner Bibee',       'Under', 4.5, -105, 0.060,  30.00, 'LOSS', 0.00),
    ('Jeffrey Springs',    'Over',  4.0, +100, 0.048,  20.00, 'WIN',  20.00),
    # Moderate (EV 7-12%) — 15 bets, 7W 8L, good CLV
    ('Shota Imanaga',      'Under', 5.5, -115, 0.107,  20.00, 'WIN',  17.39),
    ('Chase Burns',        'Over',  6.0, -110, 0.093,  20.00, 'LOSS', 0.00),
    ('Freddy Peralta',     'Under', 6.0, -130, 0.100,  20.00, 'WIN',  15.38),
    ('Lance McCullers',    'Over',  7.5, +110, 0.095,  20.00, 'LOSS', 0.00),
    ('Sandy Alcantara',    'Over',  5.5, +105, 0.082,  20.00, 'WIN',  21.00),
    ('Mitch Keller',       'Over',  3.0, -115, 0.112,  20.00, 'LOSS', 0.00),
    ('Parker Messick',     'Over',  4.0, -110, 0.088,  20.00, 'WIN',  18.18),
    ('Jack Leiter',        'Under', 6.5, -105, 0.074,  20.00, 'LOSS', 0.00),
    ('Max Meyer',          'Over',  4.5, +105, 0.097,  20.00, 'WIN',  21.00),
    ('Zac Gallen',         'Over',  2.0, -110, 0.079,  20.00, 'LOSS', 0.00),
    ('Robbie Ray',         'Over',  4.5, -105, 0.091,  20.00, 'WIN',  19.05),
    ('Eury Perez',         'Over',  5.5, +115, 0.085,  20.00, 'LOSS', 0.00),
    ('Reid Detmers',       'Over',  6.0, +105, 0.100,  20.00, 'LOSS', 0.00),
    ('Brandon Woodruff',   'Over',  5.5, +110, 0.089,  20.00, 'WIN',  22.00),
    ('Andrew Painter',     'Over',  6.5, -105, 0.076,  20.00, 'LOSS', 0.00),
    # Aggressive (EV 12-20%) — 10 bets, 4W 6L, mixed CLV
    ('Kevin Gausman',      'Under', 9.0, -105, 0.148,  5.00,  'WIN',  4.76),
    ('Randy Vasquez',      'Under', 7.0, -105, 0.133,  5.00,  'LOSS', 0.00),
    ('Tyler Glasnow',      'Over',  5.5, -110, 0.165,  5.00,  'WIN',  4.55),
    ('Shohei Ohtani',      'Over',  5.5, -110, 0.158,  5.00,  'LOSS', 0.00),
    ('Michael King',       'Over',  4.5, +115, 0.142,  5.00,  'LOSS', 0.00),
    ('Landen Roupp',       'Under', 6.5, +105, 0.136,  5.00,  'WIN',  5.25),
    ('Taj Bradley',        'Under', 5.0, -110, 0.191,  5.00,  'LOSS', 0.00),
    ('Cade Horton',        'Over',  1.5, -110, 0.185,  5.00,  'LOSS', 0.00),
    ('Drew Rasmussen',     'Over',  4.0, +115, 0.152,  5.00,  'WIN',  5.75),
    ('Joey Cantillo',      'Over',  4.5, -105, 0.138,  5.00,  'LOSS', 0.00),
    # Degen (EV 20%+) — 5 bets, flat $7 stakes, 2W 3L
    ('Tarik Skubal',       'Over',  4.0, +115, 0.613,  7.00,  'WIN',  8.05),
    ('Chris Sale',         'Over',  4.0, +110, 0.575,  7.00,  'LOSS', 0.00),
    ('Cole Ragans',        'Over',  5.5, +100, 0.454,  7.00,  'WIN',  7.00),
    ('Kris Bubic',         'Over',  3.5, +105, 0.538,  7.00,  'LOSS', 0.00),
    ('Matthew Liberatore', 'Over',  1.5, +100, 0.491,  7.00,  'LOSS', 0.00),
]

# CLV records — opening vs closing odds for each resolved bet (subset)
# Positive CLV = we got a better price than where the market settled
SYNTHETIC_CLV = [
    # Conservative — mostly positive CLV (model found edges)
    ('Garrett Crochet',    'Over',  6.5, -115, -125, 0.018),
    ('Tarik Skubal',       'Over',  5.5, -110, -120, 0.014),
    ('Cole Ragans',        'Over',  5.5, +100, -110, 0.023),
    ('Aaron Nola',         'Under', 6.5, -110, -118, 0.011),
    ('Max Fried',          'Over',  4.5, -110, -122, 0.016),
    ('Luis Castillo',      'Under', 6.0, -130, -138, 0.010),
    ('Jose Soriano',       'Under', 5.0, +105, -105, 0.028),
    ('Andrew Abbott',      'Over',  4.0, +105, +100, 0.007),
    ('Jeffrey Springs',    'Over',  4.0, +100, -108, 0.019),
    ('Logan Gilbert',      'Under', 5.5, -105, -115, 0.014),   # lost, but had CLV
    # Moderate — strong positive CLV
    ('Shota Imanaga',      'Under', 5.5, -115, -128, 0.019),
    ('Freddy Peralta',     'Under', 6.0, -130, -145, 0.017),
    ('Sandy Alcantara',    'Over',  5.5, +105, -105, 0.028),
    ('Parker Messick',     'Over',  4.0, -110, -120, 0.014),
    ('Max Meyer',          'Over',  4.5, +105, -100, 0.031),
    ('Robbie Ray',         'Over',  4.5, -105, -115, 0.014),
    ('Brandon Woodruff',   'Over',  5.5, +110, +100, 0.013),
    ('Chase Burns',        'Over',  6.0, -110, -120, 0.014),   # lost, had CLV
    # Aggressive — mixed CLV
    ('Kevin Gausman',      'Under', 9.0, -105, -110, 0.007),
    ('Tyler Glasnow',      'Over',  5.5, -110, -118, 0.011),
    ('Landen Roupp',       'Under', 6.5, +105, +100, 0.007),
    ('Drew Rasmussen',     'Over',  4.0, +115, +105, 0.013),
    ('Randy Vasquez',      'Under', 7.0, -105, -100, -0.007),  # negative CLV
    ('Shohei Ohtani',      'Over',  5.5, -110, -105, -0.007),  # negative CLV
    # Degen — volatile, some negative CLV expected
    ('Tarik Skubal',       'Over',  4.0, +115, +110, 0.006),
    ('Cole Ragans',        'Over',  5.5, +100, +105, -0.007),
    ('Chris Sale',         'Over',  4.0, +110, +115, -0.006),
]


# ─────────────────────────────────────────────
# Inject / clean up helpers
# ─────────────────────────────────────────────

def _inject_trades():
    """Append synthetic resolved trades to paper_trades.csv."""
    existing_lines = []
    has_file = os.path.exists(TRADES_PATH)
    if has_file:
        with open(TRADES_PATH, 'r', encoding='utf-8') as f:
            existing_lines = f.readlines()

    bankroll = 715.00  # start from current position
    injected = 0

    with open(TRADES_PATH, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
        if not has_file:
            writer.writeheader()

        for i, (player, side, line, odds, ev, stake, result, payout) in enumerate(SYNTHETIC_TRADES):
            net = round(payout - stake, 2) if result == 'WIN' else round(-stake, 2)
            date_str = (datetime(2026, 3, 25) + timedelta(days=i % 14)).strftime('%Y-%m-%d %H:%M')
            writer.writerow({
                'date':            date_str,
                'player':          f'{player}{TEST_TAG}',
                'prop_type':       'pitcher_strikeouts',
                'side':            side,
                'line':            line,
                'odds':            odds,
                'ev':              ev,
                'stake':           stake,
                'bankroll_before': bankroll,
                'bankroll_after':  round(bankroll + net, 2),
                'result':          result,
                'payout':          payout,
                'net':             net,
                'matchup':         'Test @ Test',
                'book':            'Synthetic',
                'postpone_count':  0,
            })
            bankroll = round(bankroll + net, 2)
            injected += 1

    print(f'  Injected {injected} synthetic trades into paper_trades.csv')
    return injected


def _inject_clv():
    """Insert synthetic CLV records into Supabase closing_lines."""
    try:
        from database import get_client
        client = get_client()
        rows = []
        for i, (player, side, line, open_odds, close_odds, clv) in enumerate(SYNTHETIC_CLV):
            date_str = (datetime(2026, 3, 25) + timedelta(days=i % 14)).strftime('%Y-%m-%d')
            rows.append({
                'date':         date_str,
                'player':       f'{player}{TEST_TAG}',
                'prop_type':    'pitcher_strikeouts',
                'line':         line,
                'side':         side,
                'opening_odds': open_odds,
                'closing_odds': close_odds,
                'book':         'Synthetic',
                'clv_pct':      clv,
            })
        client.table('closing_lines').insert(rows).execute()
        print(f'  Inserted {len(rows)} synthetic CLV records into Supabase')
    except Exception as e:
        print(f'  CLV inject failed: {e}')


def _cleanup_trades():
    """Remove all synthetic rows from paper_trades.csv."""
    if not os.path.exists(TRADES_PATH):
        return
    import pandas as pd
    df = pd.read_csv(TRADES_PATH)
    before = len(df)
    df = df[~df['player'].astype(str).str.endswith(TEST_TAG)]
    df.to_csv(TRADES_PATH, index=False)
    print(f'  Removed {before - len(df)} synthetic rows from paper_trades.csv')


def _cleanup_clv():
    """Remove synthetic CLV rows from Supabase."""
    try:
        from database import get_client
        client = get_client()
        resp = client.table('closing_lines').delete().like('player', f'%{TEST_TAG}').execute()
        print(f'  Cleaned up synthetic CLV records from Supabase')
    except Exception as e:
        print(f'  CLV cleanup failed: {e}')


# ─────────────────────────────────────────────
# Patched stats loader (reads TEST_TAG rows only)
# ─────────────────────────────────────────────

def _run_with_synthetic():
    """
    Monkey-patch the dashboard's data loaders to return only synthetic
    rows, so real pending trades are unaffected in the output.
    """
    import pandas as pd
    from database import get_client

    # Load only synthetic trades
    df = pd.read_csv(TRADES_PATH)
    df = df[df['player'].astype(str).str.endswith(TEST_TAG)].copy()
    df['player'] = df['player'].str.replace(TEST_TAG, '', regex=False)
    for col in ['stake', 'payout', 'net', 'ev', 'odds']:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

    # Load only synthetic CLV
    try:
        client = get_client()
        resp = client.table('closing_lines').select('*').like('player', f'%{TEST_TAG}').execute()
        clv_df = pd.DataFrame(resp.data or [])
        if not clv_df.empty:
            clv_df['player'] = clv_df['player'].str.replace(TEST_TAG, '', regex=False)
            clv_df['clv_pct'] = pd.to_numeric(clv_df['clv_pct'], errors='coerce')
    except Exception:
        clv_df = pd.DataFrame()

    # Inline the stats computation (same logic as readiness_dashboard.compute_readiness_stats)
    from alerts.readiness_dashboard import compute_readiness_stats, print_dashboard

    # Patch _load_trades and _load_clv temporarily
    import alerts.readiness_dashboard as rd
    original_trades = rd._load_trades
    original_clv    = rd._load_clv

    rd._load_trades = lambda: df
    rd._load_clv    = lambda: clv_df

    stats = compute_readiness_stats()
    print_dashboard(stats)

    # Restore originals
    rd._load_trades = original_trades
    rd._load_clv    = original_clv


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

if __name__ == '__main__':
    print()
    print('=' * 58)
    print('  READINESS DASHBOARD — SYNTHETIC DATA TEST')
    print('=' * 58)
    print(f'  {len(SYNTHETIC_TRADES)} trades  |  {len(SYNTHETIC_CLV)} CLV records')
    print()

    print('Injecting synthetic data...')
    _inject_trades()
    _inject_clv()

    print('\nRunning dashboard on synthetic data only...')
    _run_with_synthetic()

    print('Cleaning up...')
    _cleanup_trades()
    _cleanup_clv()
    print('  Done — no test data left behind.')

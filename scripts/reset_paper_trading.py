"""
scripts/reset_paper_trading.py — One-time paper trading reset.

Wipes all bet history, signals, and pipeline logs so the next run
starts from a clean slate with a fresh $1,000 bankroll.

Run manually:  python scripts/reset_paper_trading.py
DO NOT schedule or add to main.py.

Tables cleared:   paper_trades, ev_signals, closing_lines,
                  pipeline_runs, readiness_history, line_movement
Files deleted:    data/processed/paper_trades.csv
                  data/processed/ev_signals.csv
Tables preserved: team_krates_cache, player_baselines_cache,
                  umpire_profiles, statcast_pull_log
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import BANKROLL

PROCESSED_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'processed')

CLEAR_TABLES = [
    'paper_trades',
    'ev_signals',
    'closing_lines',
    'pipeline_runs',
    'readiness_history',
]
OPTIONAL_CLEAR_TABLES = [
    'line_movement',  # only if it exists
]
PRESERVE_TABLES = [
    'team_krates_cache',
    'player_baselines_cache',
    'umpire_profiles',
    'statcast_pull_log',
]
LOCAL_FILES = [
    os.path.join(PROCESSED_DIR, 'paper_trades.csv'),
    os.path.join(PROCESSED_DIR, 'ev_signals.csv'),
]

STARTING_BANKROLL = float(BANKROLL) if BANKROLL else 1000.0


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _get_client():
    from database import get_client
    client = get_client()
    if client is None:
        print('\nERROR: Could not connect to Supabase.')
        print('  Make sure SUPABASE_URL and SUPABASE_KEY are set in your .env file.')
        sys.exit(1)
    return client


def _count_rows(client, table: str) -> int:
    """Return the number of rows in a table, or -1 if the table doesn't exist."""
    try:
        resp = client.table(table).select('id').execute()
        return len(resp.data)
    except Exception:
        return -1


def _delete_all(client, table: str) -> int:
    """Delete all rows from a table. Returns number of rows deleted."""
    count_before = _count_rows(client, table)
    if count_before < 0:
        return -1   # table doesn't exist
    if count_before == 0:
        return 0    # already empty
    client.table(table).delete().neq('id', 0).execute()
    return count_before


# ─────────────────────────────────────────────
# Safety prompt
# ─────────────────────────────────────────────

print()
print('=' * 60)
print('  PLAYBOOK -- Paper Trading Reset')
print('=' * 60)
print()
print('  This will permanently delete ALL paper trading history:')
print('    - All paper bets (paper_trades)')
print('    - All EV signals (ev_signals)')
print('    - All closing line records (closing_lines)')
print('    - All pipeline run logs (pipeline_runs)')
print('    - All readiness history (readiness_history)')
print('    - All line movement records (line_movement)')
print('    - Local CSV files: paper_trades.csv, ev_signals.csv')
print()
print('  Historical data (baselines, K-rates, umpires) is NOT touched.')
print()

confirm = input('  Type YES to confirm: ').strip()
if confirm != 'YES':
    print()
    print('  Cancelled — nothing was deleted.')
    sys.exit(0)

print()
print('  Confirmed. Starting reset...')
print()

client = _get_client()

deleted_counts   = {}   # table -> rows deleted
skipped_tables   = []   # optional tables that didn't exist
preserved_counts = {}   # table -> current row count
deleted_files    = []
skipped_files    = []


# ─────────────────────────────────────────────
# Step 1 — Clear Supabase tables
# ─────────────────────────────────────────────

print('Step 1 — Clearing Supabase tables...')

for table in CLEAR_TABLES:
    n = _delete_all(client, table)
    if n < 0:
        print(f'  WARNING: table "{table}" not found — skipping.')
        skipped_tables.append(table)
    else:
        deleted_counts[table] = n
        print(f'  {table:<25} {n:>4} row(s) deleted')

for table in OPTIONAL_CLEAR_TABLES:
    n = _delete_all(client, table)
    if n < 0:
        print(f'  {table:<25}  (table not found — skipping)')
        skipped_tables.append(table)
    else:
        deleted_counts[table] = n
        print(f'  {table:<25} {n:>4} row(s) deleted')

print()


# ─────────────────────────────────────────────
# Step 2 — Clear local processed files
# ─────────────────────────────────────────────

print('Step 2 — Clearing local processed files...')

for path in LOCAL_FILES:
    filename = os.path.basename(path)
    if os.path.exists(path):
        os.remove(path)
        deleted_files.append(filename)
        print(f'  Deleted: {filename}')
    else:
        skipped_files.append(filename)
        print(f'  Not found (already clean): {filename}')

print()


# ─────────────────────────────────────────────
# Step 3 — Bankroll reset note
# ─────────────────────────────────────────────

print('Step 3 — Bankroll reset...')
print(f'  paper_trades is now empty. The next pipeline run will')
print(f'  automatically start with a fresh ${STARTING_BANKROLL:,.2f} bankroll')
print(f'  (set via BANKROLL in your .env file).')
print()


# ─────────────────────────────────────────────
# Step 4 — Verify preserved tables
# ─────────────────────────────────────────────

print('Step 4 — Verifying preserved tables (not touched)...')

for table in PRESERVE_TABLES:
    n = _count_rows(client, table)
    if n < 0:
        preserved_counts[table] = None
        print(f'  {table:<28}  (table not found)')
    else:
        preserved_counts[table] = n
        print(f'  {table:<28} {n:>4} row(s) intact')

print()


# ─────────────────────────────────────────────
# Step 5 — Summary
# ─────────────────────────────────────────────

print('=' * 60)
print('  RESET SUMMARY')
print('=' * 60)

print()
print('  TABLES CLEARED:')
for table, n in deleted_counts.items():
    print(f'    {table:<28} {n:>4} row(s) removed')
if skipped_tables:
    for table in skipped_tables:
        print(f'    {table:<28} (not found — skipped)')

print()
print('  FILES DELETED:')
if deleted_files:
    for f in deleted_files:
        print(f'    {f}')
else:
    print('    None (files were already absent)')
if skipped_files:
    for f in skipped_files:
        print(f'    {f} (not found — skipped)')

print()
print('  TABLES PRESERVED:')
for table, n in preserved_counts.items():
    count_str = f'{n} row(s)' if n is not None else 'not found'
    print(f'    {table:<28} {count_str}')

print()
print('  Clean slate ready — run main.py tomorrow morning.')
print()

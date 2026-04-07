"""Injects synthetic data, sends readiness dashboard to Discord, then cleans up."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Re-use all the helpers from the test file
from tests.test_readiness_dashboard import (
    _inject_trades, _inject_clv, _cleanup_trades, _cleanup_clv,
    TEST_TAG, TRADES_PATH
)
import pandas as pd
import alerts.readiness_dashboard as rd
from database import get_client

print('Injecting synthetic data...')
_inject_trades()
_inject_clv()

# Load only the synthetic rows
df_syn = pd.read_csv(TRADES_PATH)
df_syn = df_syn[df_syn['player'].astype(str).str.endswith(TEST_TAG)].copy()
df_syn['player'] = df_syn['player'].str.replace(TEST_TAG, '', regex=False)
for col in ['stake', 'payout', 'net', 'ev', 'odds']:
    df_syn[col] = pd.to_numeric(df_syn[col], errors='coerce').fillna(0.0)

client = get_client()
resp = client.table('closing_lines').select('*').like('player', f'%{TEST_TAG}%').execute()
clv_df = pd.DataFrame(resp.data or [])
if not clv_df.empty:
    clv_df['player'] = clv_df['player'].str.replace(TEST_TAG, '', regex=False)
    clv_df['clv_pct'] = pd.to_numeric(clv_df['clv_pct'], errors='coerce')

# Patch loaders, compute, print, and send to Discord
original_trades, original_clv = rd._load_trades, rd._load_clv
rd._load_trades = lambda: df_syn
rd._load_clv    = lambda: clv_df

stats = rd.compute_readiness_stats()
rd.print_dashboard(stats)
rd.send_dashboard(stats)   # <-- hits Discord

rd._load_trades = original_trades
rd._load_clv    = original_clv

print('\nCleaning up...')
_cleanup_trades()
_cleanup_clv()
print('Done.')

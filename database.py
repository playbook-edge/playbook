"""
database.py — Supabase client and write functions for Playbook.

Three tables (created in Supabase SQL editor):
  ev_signals    — every flagged EV signal found by the model, every day
  paper_trades  — every paper bet placed and its result
  pipeline_runs — log of each daily pipeline execution

All functions degrade gracefully: if SUPABASE_URL / SUPABASE_KEY are not
set, or if the network call fails, they print a warning and return without
crashing the pipeline.
"""

import os
import sys
import math
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from config import SUPABASE_URL, SUPABASE_KEY


# ─────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────

def get_client():
    """
    Return a Supabase client, or None if credentials are missing.
    Called fresh each time so we never hold a stale connection.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f'  Supabase client error: {e}')
        return None


# ─────────────────────────────────────────────
# Serialisation helper
# ─────────────────────────────────────────────

def _clean(val):
    """
    Convert a value to a JSON-safe Python native type for Supabase.
    Handles: pandas NA, numpy int64/float64, NaN, Inf → None or native type.
    """
    if val is None:
        return None

    # pandas NA / NaT
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass

    # numpy numeric types
    try:
        import numpy as np
        if isinstance(val, np.integer):
            return int(val)
        if isinstance(val, np.floating):
            val = float(val)
        if isinstance(val, np.bool_):
            return bool(val)
    except ImportError:
        pass

    # Python float NaN / Inf
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None

    return val


# ─────────────────────────────────────────────
# ev_signals
# ─────────────────────────────────────────────

def log_ev_signals(signals_df: pd.DataFrame, run_date=None) -> int:
    """
    Write all flagged EV signals (ev >= 4%) to the ev_signals table.
    Called automatically after ev_calculator runs.
    Returns number of rows inserted, or 0 if Supabase is unavailable.
    """
    client = get_client()
    if client is None:
        print('  Supabase not configured — skipping ev_signals log.')
        return 0

    today   = str(run_date or date.today())
    flagged = signals_df[signals_df['flag'] == True].copy()

    if flagged.empty:
        print('  No flagged signals to log.')
        return 0

    rows = []
    for _, r in flagged.iterrows():
        rows.append({
            'run_date':         today,
            'player':           _clean(r.get('player')),
            'prop_type':        _clean(r.get('prop_type')),
            'matchup':          _clean(r.get('matchup')),
            'book':             _clean(r.get('book')),
            'side':             _clean(r.get('side')),
            'line':             _clean(r.get('line')),
            'odds':             int(float(_clean(r.get('odds')))) if _clean(r.get('odds')) is not None else None,
            'ev':               _clean(r.get('ev')),
            'model_prob':       _clean(r.get('model_prob')),
            'implied_prob':     _clean(r.get('implied_prob')),
            'kelly_pct':        _clean(r.get('kelly_pct')),
            'kelly_dollars':    _clean(r.get('kelly_dollars')),
            'k9_used':          _clean(r.get('k9_used')),
            'k9_current':       _clean(r.get('k9_current')),
            'k9_historical':    _clean(r.get('k9_historical')),
            'k9_trend':         _clean(r.get('k9_trend')),
            'hist_reliability': int(float(_clean(r.get('hist_reliability')))) if _clean(r.get('hist_reliability')) is not None else None,
            'ip_per_start':     _clean(r.get('ip_per_start')),
            'xfip':             _clean(r.get('xfip')),
            'opp_team':         _clean(r.get('opp_team')),
            'opp_k_pct':        _clean(r.get('opp_k_pct')),
            'matchup_factor':   _clean(r.get('matchup_factor')),
            'expected_ks':      _clean(r.get('expected_ks')),
            'flag':             True,
        })

    try:
        client.table('ev_signals').insert(rows).execute()
        print(f'  Logged {len(rows)} signal(s) to Supabase.')
        return len(rows)
    except Exception as e:
        print(f'  Supabase ev_signals error: {e}')
        return 0


# ─────────────────────────────────────────────
# paper_trades
# ─────────────────────────────────────────────

def log_paper_trade(trade: dict):
    """
    Insert a single paper trade into the paper_trades table.
    Called when a new bet is placed by log_bets_from_signals().
    """
    client = get_client()
    if client is None:
        return

    row = {
        'trade_date':      _clean(trade.get('date')),
        'player':          _clean(trade.get('player')),
        'prop_type':       _clean(trade.get('prop_type')),
        'side':            _clean(trade.get('side')),
        'line':            _clean(trade.get('line')),
        'odds':            int(_clean(trade.get('odds'))) if _clean(trade.get('odds')) is not None else None,
        'ev':              _clean(trade.get('ev')),
        'stake':           _clean(trade.get('stake')),
        'bankroll_before': _clean(trade.get('bankroll_before')),
        'bankroll_after':  _clean(trade.get('bankroll_after')),
        'result':          'PENDING',
        'payout':          0.0,
        'net':             0.0,
        'matchup':         _clean(trade.get('matchup')),
        'book':            _clean(trade.get('book')),
    }

    try:
        client.table('paper_trades').insert(row).execute()
    except Exception as e:
        print(f'  Supabase paper_trade insert error: {e}')


def get_pending_trades() -> list:
    """
    Fetch all PENDING paper trades from Supabase.
    Used by auto_resolve on Railway where paper_trades.csv doesn't persist.
    Returns a list of dicts, or [] if Supabase is unavailable.
    """
    client = get_client()
    if client is None:
        return []
    try:
        resp = client.table('paper_trades').select('*').eq('result', 'PENDING').execute()
        return resp.data or []
    except Exception as e:
        print(f'  Supabase get_pending_trades error: {e}')
        return []


def update_paper_trade_result(player: str, trade_date: str, side: str,
                               line: float, result: str,
                               payout: float, net: float):
    """
    Mark a PENDING paper trade as WIN or LOSS after the game resolves.
    Matches on player + date + side + line since we don't store the DB id locally.
    """
    client = get_client()
    if client is None:
        return

    try:
        (client.table('paper_trades')
               .update({'result': result, 'payout': payout, 'net': net})
               .eq('player', player)
               .eq('side', side)
               .eq('line', float(line))
               .eq('result', 'PENDING')
               .gte('trade_date', str(trade_date)[:10])
               .execute())
    except Exception as e:
        print(f'  Supabase paper_trade update error: {e}')


# ─────────────────────────────────────────────
# pipeline_runs
# ─────────────────────────────────────────────

def log_pipeline_run(run_date, steps_passed: int, steps_failed: int,
                     notes: str = ''):
    """
    Record a completed pipeline run.
    Called at the end of main.py so you have a full history of daily runs.
    """
    client = get_client()
    if client is None:
        return

    try:
        client.table('pipeline_runs').insert({
            'run_date':     str(run_date),
            'steps_passed': steps_passed,
            'steps_failed': steps_failed,
            'notes':        notes,
        }).execute()
    except Exception as e:
        print(f'  Supabase pipeline_run error: {e}')

"""
database.py — Supabase client and write functions for Playbook.

Four tables (created in Supabase SQL editor):
  ev_signals    — every flagged EV signal found by the model, every day
  paper_trades  — every paper bet placed and its result
  closing_lines — closing odds captured at resolve time for CLV tracking
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
    Write all flagged EV signals (ev >= 2%) to the ev_signals table.
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
            'run_date':              today,
            'player':                _clean(r.get('player')),
            'prop_type':             _clean(r.get('prop_type')),
            'matchup':               _clean(r.get('matchup')),
            'book':                  _clean(r.get('book')),
            'side':                  _clean(r.get('side')),
            'line':                  _clean(r.get('line')),
            'odds':                  int(float(_clean(r.get('odds')))) if _clean(r.get('odds')) is not None else None,
            'ev':                    _clean(r.get('ev')),
            'model_prob':            _clean(r.get('model_prob')),
            'implied_prob':          _clean(r.get('implied_prob')),
            'kelly_pct':             _clean(r.get('kelly_pct')),
            'kelly_dollars':         _clean(r.get('kelly_dollars')),
            'k9_used':               _clean(r.get('k9_used')),
            'k9_current':            _clean(r.get('k9_current')),
            'k9_historical':         _clean(r.get('k9_historical')),
            'k9_trend':              _clean(r.get('k9_trend')),
            'hist_reliability':      int(float(_clean(r.get('hist_reliability')))) if _clean(r.get('hist_reliability')) is not None else None,
            'ip_per_start':          _clean(r.get('ip_per_start')),
            'xfip':                  _clean(r.get('xfip')),
            'opp_team':              _clean(r.get('opp_team')),
            'opp_k_pct':             _clean(r.get('opp_k_pct')),
            'matchup_factor':        _clean(r.get('matchup_factor')),
            'expected_ks':           _clean(r.get('expected_ks')),
            'flag':                  True,
            # Calibration columns
            'velo_trend':            _clean(r.get('velo_trend')),
            'velo_factor':           _clean(r.get('velo_factor')),
            'spin_rate':             _clean(r.get('spin_rate')),
            'pitch_mix':             _clean(r.get('pitch_mix')),
            'throws':                _clean(r.get('throws')),
            'prob_capped':           bool(_clean(r.get('prob_capped'))) if _clean(r.get('prob_capped')) is not None else False,
            'low_line_note':         _clean(r.get('low_line_note')),
            'umpire_name':           _clean(r.get('umpire_name')),
            'umpire_adjustment':     _clean(r.get('umpire_adjustment')),
            'kelly_cap_applied':     bool(_clean(r.get('kelly_cap_applied'))) if _clean(r.get('kelly_cap_applied')) is not None else False,
            'low_history':           bool(_clean(r.get('low_history'))) if _clean(r.get('low_history')) is not None else False,
            'ev_suspect':            bool(_clean(r.get('ev_suspect'))) if _clean(r.get('ev_suspect')) is not None else False,
            'duplicate':             bool(_clean(r.get('duplicate'))) if _clean(r.get('duplicate')) is not None else False,
            'innings_capped':        bool(_clean(r.get('innings_capped'))) if _clean(r.get('innings_capped')) is not None else False,
            'matchup_pa_count':      int(_clean(r.get('matchup_pa_count'))) if _clean(r.get('matchup_pa_count')) is not None else None,
            # Weather columns
            'weather_wind_label':    _clean(r.get('weather_wind_label')),
            'weather_wind_factor':   _clean(r.get('weather_wind_factor')),
            'weather_temp_f':        _clean(r.get('weather_temp_f')),
            'weather_precip_pct':    int(_clean(r.get('weather_precip_pct'))) if _clean(r.get('weather_precip_pct')) is not None else None,
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


def update_postpone_count(player: str, trade_date: str, side: str,
                          line: float, new_count: int):
    """
    Set the postpone_count on a PENDING paper trade.
    Called each time a game is found to be Postponed, Suspended, or Cancelled.
    """
    client = get_client()
    if client is None:
        return
    try:
        (client.table('paper_trades')
               .update({'postpone_count': new_count})
               .eq('player', player)
               .eq('side', side)
               .eq('line', float(line))
               .eq('result', 'PENDING')
               .gte('trade_date', str(trade_date)[:10])
               .execute())
    except Exception as e:
        print(f'  Supabase postpone_count update error: {e}')


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
# umpire_profiles
# ─────────────────────────────────────────────

def save_umpire_profiles(profiles: list) -> int:
    """
    Overwrite the umpire_profiles table with a fresh set of profiles.
    Called by umpire_scraper.build_umpire_profiles() on weekly refresh.
    Returns the number of rows inserted.
    """
    client = get_client()
    if client is None:
        return 0
    try:
        client.table('umpire_profiles').delete().neq('id', 0).execute()
        rows = [
            {
                'umpire_name':            _clean(p.get('umpire_name')),
                'umpire_id':              _clean(p.get('umpire_id')),
                'zone_size_pct':          int(p['zone_size_pct']) if p.get('zone_size_pct') is not None else None,
                'k_per_game':             _clean(p.get('k_per_game')),
                'runs_per_game':          _clean(p.get('runs_per_game')),
                'first_pitch_strike_pct': _clean(p.get('first_pitch_strike_pct')),
                'last_updated':           _clean(p.get('last_updated')),
            }
            for p in profiles
        ]
        client.table('umpire_profiles').insert(rows).execute()
        return len(rows)
    except Exception as e:
        print(f'  Supabase umpire_profiles save error: {e}')
        return 0


def get_umpire_profiles() -> list:
    """Return all rows from umpire_profiles as a list of dicts."""
    client = get_client()
    if client is None:
        return []
    try:
        resp = client.table('umpire_profiles').select('*').execute()
        return resp.data or []
    except Exception as e:
        print(f'  Supabase umpire_profiles read error: {e}')
        return []


def get_umpire_last_updated() -> str | None:
    """Return the most recent last_updated date in umpire_profiles, or None."""
    client = get_client()
    if client is None:
        return None
    try:
        resp = (client.table('umpire_profiles')
                      .select('last_updated')
                      .order('last_updated', desc=True)
                      .limit(1)
                      .execute())
        if resp.data:
            return resp.data[0]['last_updated']
        return None
    except Exception as e:
        print(f'  Supabase umpire last_updated check error: {e}')
        return None


# ─────────────────────────────────────────────
# closing_lines
# ─────────────────────────────────────────────

def log_closing_line(record: dict):
    """
    Insert one closing line record into the closing_lines table.

    Expected keys:
      date          — trade date (YYYY-MM-DD)
      player        — pitcher name
      prop_type     — 'pitcher_strikeouts' or 'pitcher_innings'
      line          — prop line (e.g. 5.5)
      side          — 'Over' or 'Under'
      opening_odds  — American odds we bet at (e.g. -110)
      closing_odds  — American odds at market close (None if unavailable)
      book          — sportsbook name
      clv_pct       — closing_implied_prob - opening_implied_prob
                      Positive = we got a better price than the closing line.
                      None if closing_odds is unavailable.
    """
    client = get_client()
    if client is None:
        return

    row = {
        'date':         _clean(record.get('date')),
        'player':       _clean(record.get('player')),
        'prop_type':    _clean(record.get('prop_type')),
        'line':         _clean(record.get('line')),
        'side':         _clean(record.get('side')),
        'opening_odds': _clean(record.get('opening_odds')),
        'closing_odds': _clean(record.get('closing_odds')),
        'book':         _clean(record.get('book')),
        'clv_pct':      _clean(record.get('clv_pct')),
    }

    try:
        client.table('closing_lines').insert(row).execute()
    except Exception as e:
        print(f'  Supabase closing_lines insert error: {e}')


# ─────────────────────────────────────────────
# line_movement
# ─────────────────────────────────────────────

def log_line_movement(record: dict):
    """
    Insert one line movement record into the line_movement table.
    Called by capture_line_movement() in paper_trading.py at the 6:30 PM snapshot.

    Columns: date, player, prop_type, side, line,
             opening_odds, snapshot_odds,
             opening_implied, snapshot_implied,
             movement_pct, movement_direction
    """
    client = get_client()
    if client is None:
        return

    row = {
        'date':               _clean(record.get('date')),
        'player':             _clean(record.get('player')),
        'prop_type':          _clean(record.get('prop_type')),
        'side':               _clean(record.get('side')),
        'line':               _clean(record.get('line')),
        'opening_odds':       int(record['opening_odds'])   if record.get('opening_odds')  is not None else None,
        'snapshot_odds':      int(record['snapshot_odds'])  if record.get('snapshot_odds') is not None else None,
        'opening_implied':    _clean(record.get('opening_implied')),
        'snapshot_implied':   _clean(record.get('snapshot_implied')),
        'movement_pct':       _clean(record.get('movement_pct')),
        'movement_direction': _clean(record.get('movement_direction')),
    }

    try:
        client.table('line_movement').insert(row).execute()
    except Exception as e:
        print(f'  Supabase line_movement insert error: {e}')


# ─────────────────────────────────────────────
# pipeline_runs
# ─────────────────────────────────────────────

def log_pipeline_run(run_date, steps_passed: int, steps_failed: int,
                     notes: str = '', odds_api_quota: int = None):
    """
    Record a completed pipeline run.
    Called at the end of main.py so you have a full history of daily runs.
    odds_api_quota: remaining API requests this month, captured from the scraper.
    """
    client = get_client()
    if client is None:
        return

    # Append quota to notes so it's visible even without a schema migration
    if odds_api_quota is not None:
        quota_note = f'odds_api_quota:{odds_api_quota}'
        notes = f'{notes} | {quota_note}' if notes else quota_note

    try:
        client.table('pipeline_runs').insert({
            'run_date':     str(run_date),
            'steps_passed': steps_passed,
            'steps_failed': steps_failed,
            'notes':        notes,
        }).execute()
    except Exception as e:
        print(f'  Supabase pipeline_run error: {e}')

"""
tools/migrate_ev_signals.py — Add missing columns to the Supabase ev_signals table.

Run once: python tools/migrate_ev_signals.py

Tries to execute the migration automatically.
If that fails, prints the SQL to paste into Supabase Dashboard > SQL Editor.
"""

import os
import sys
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import SUPABASE_URL, SUPABASE_KEY

# ── Migration SQL ────────────────────────────────────────────────────────────
# ADD COLUMN IF NOT EXISTS is safe to re-run — skips columns that already exist.

MIGRATION_SQL = """
ALTER TABLE ev_signals
  ADD COLUMN IF NOT EXISTS velo_trend          FLOAT,
  ADD COLUMN IF NOT EXISTS velo_factor         FLOAT,
  ADD COLUMN IF NOT EXISTS spin_rate           FLOAT,
  ADD COLUMN IF NOT EXISTS pitch_mix           TEXT,
  ADD COLUMN IF NOT EXISTS throws              TEXT,
  ADD COLUMN IF NOT EXISTS prob_capped         BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS low_line_note       TEXT,
  ADD COLUMN IF NOT EXISTS umpire_name         TEXT,
  ADD COLUMN IF NOT EXISTS umpire_adjustment   FLOAT,
  ADD COLUMN IF NOT EXISTS kelly_cap_applied   BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS low_history         BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS ev_suspect          BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS weather_wind_label  TEXT,
  ADD COLUMN IF NOT EXISTS weather_wind_factor FLOAT,
  ADD COLUMN IF NOT EXISTS weather_temp_f      FLOAT,
  ADD COLUMN IF NOT EXISTS weather_precip_pct  INTEGER;
""".strip()


def _print_sql_instructions():
    print()
    print('-' * 60)
    print('  MANUAL MIGRATION -- paste this into Supabase SQL Editor:')
    print('  Dashboard > SQL Editor > New query > Run')
    print('-' * 60)
    print()
    print(MIGRATION_SQL)
    print()
    print('-' * 60)


def run():
    print('=' * 60)
    print('  Supabase Migration — ev_signals new columns')
    print('=' * 60)

    if not SUPABASE_URL or not SUPABASE_KEY:
        print('ERROR: SUPABASE_URL or SUPABASE_KEY not set in .env')
        _print_sql_instructions()
        return

    # ── Attempt 1: via supabase-py rpc (works if exec_sql function exists) ──
    try:
        from supabase import create_client
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        client.rpc('exec_sql', {'query': MIGRATION_SQL}).execute()
        print('Migration applied via rpc exec_sql.')
        _verify(client)
        return
    except Exception:
        pass   # function probably doesn't exist — try next approach

    # ── Attempt 2: via Supabase Management REST API ──────────────────────────
    # Works when SUPABASE_KEY is the service_role key.
    # Endpoint: POST /rest/v1/  with Prefer: resolution=merge-duplicates
    # (not standard — try the pg query path instead)
    try:
        # Extract project ref from URL: https://xxxx.supabase.co → xxxx
        project_ref = SUPABASE_URL.replace('https://', '').split('.')[0]
        mgmt_url    = f'https://api.supabase.com/v1/projects/{project_ref}/database/query'
        headers     = {
            'Authorization': f'Bearer {SUPABASE_KEY}',
            'Content-Type':  'application/json',
        }
        resp = requests.post(mgmt_url, headers=headers,
                             json={'query': MIGRATION_SQL}, timeout=15)
        if resp.status_code in (200, 201):
            print('Migration applied via Management API.')
            from supabase import create_client
            _verify(create_client(SUPABASE_URL, SUPABASE_KEY))
            return
        # Not an auth error — fall through to manual instructions
    except Exception:
        pass

    # ── Fallback: print SQL for manual execution ─────────────────────────────
    print()
    print('Automatic migration could not run (key may not have DDL permissions).')
    print('This is normal — Supabase anon keys cannot alter the schema.')
    _print_sql_instructions()


def _verify(client):
    """Pull one row and print the new column names to confirm they exist."""
    try:
        res = client.table('ev_signals').select('low_history,ev_suspect,weather_wind_label').limit(1).execute()
        print('Verified: low_history, ev_suspect, weather_wind_label all present.')
    except Exception as e:
        print(f'Verification failed: {e}')
        print('The columns may not have been created — run the SQL manually.')
        _print_sql_instructions()


if __name__ == '__main__':
    run()

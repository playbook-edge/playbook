"""
main.py — Playbook daily pipeline.

Runs every step in order:
  1. baseball_savant.py  — today's starters + Statcast stats
  2. fangraphs.py        — team K-rates + pitcher leaderboard
  3. odds_api.py         — live prop lines from DK / FD / BetMGM
  4. ev_calculator.py    — find edges, calculate EV, fire Discord alerts

Schedule this with Windows Task Scheduler to run automatically each day.
Or run manually: python main.py
"""

import os
import sys
import time
import traceback
from datetime import datetime

# ── Make sure imports resolve from the project root ──────────
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import config

LOG_DIR  = os.path.join(ROOT, 'logs')
LOG_FILE = os.path.join(LOG_DIR, f'pipeline_{datetime.now().strftime("%Y-%m-%d")}.log')


# ─────────────────────────────────────────────────────────────
# Logging — writes to both terminal and a daily log file
# ─────────────────────────────────────────────────────────────

class Logger:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.file = open(path, 'a', encoding='utf-8')

    def log(self, msg=''):
        timestamp = datetime.now().strftime('%H:%M:%S')
        line = f'[{timestamp}] {msg}'
        print(line)
        self.file.write(line + '\n')
        self.file.flush()

    def close(self):
        self.file.close()


# ─────────────────────────────────────────────────────────────
# Step runner — isolates each script so one failure doesn't
#               stop the whole pipeline
# ─────────────────────────────────────────────────────────────

def run_step(logger, name, fn):
    logger.log(f'--- {name} ---')
    start = time.time()
    try:
        fn()
        elapsed = round(time.time() - start, 1)
        logger.log(f'    Done ({elapsed}s)')
        return {'ok': True, 'note': f'completed in {elapsed}s'}
    except Exception as e:
        logger.log(f'    FAILED: {e}')
        logger.log(traceback.format_exc())
        return {'ok': False, 'note': str(e)[:80]}


# ─────────────────────────────────────────────────────────────
# Pipeline steps
# ─────────────────────────────────────────────────────────────

def step_savant():
    from scrapers.baseball_savant import run
    run()

def step_fangraphs():
    from scrapers.fangraphs import run
    run()

def step_historical():
    from scrapers.historical_stats import run
    run()

def step_baseline():
    from models.player_baseline import run
    run()

def step_odds():
    from scrapers.odds_api import run
    run()

def step_ev():
    from models.ev_calculator import run
    run()


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    logger = Logger(LOG_FILE)
    logger.log('=' * 50)
    logger.log(f'PLAYBOOK PIPELINE START — {datetime.now().strftime("%A, %B %d %Y")}')
    logger.log('=' * 50)

    # Config check
    missing = [k for k, v in {
        'ODDS_API_KEY':                config.ODDS_API_KEY,
        'DISCORD_WEBHOOK_CONSERVATIVE': config.DISCORD_WEBHOOK_CONSERVATIVE,
    }.items() if not v]

    if missing:
        logger.log(f'WARNING: Missing keys in .env: {", ".join(missing)}')
    else:
        logger.log('Config OK')

    pipeline_start = time.time()
    results        = {}

    steps = [
        ('Baseball Savant',  'Step 1/6  Baseball Savant',  step_savant),
        ('FanGraphs',        'Step 2/6  FanGraphs',        step_fangraphs),
        ('Historical Stats', 'Step 3/6  Historical Stats', step_historical),
        ('Player Baselines', 'Step 4/6  Player Baselines', step_baseline),
        ('Odds API',         'Step 5/6  Odds API',         step_odds),
        ('EV Calculator',    'Step 6/6  EV + Alerts',      step_ev),
    ]

    for key, name, fn in steps:
        result = run_step(logger, name, fn)
        results[key] = result
        if not result['ok']:
            try:
                from alerts.discord_alerts import send_error_alert
                send_error_alert(key, result['note'])
            except Exception as e:
                logger.log(f'  Health error alert failed: {e}')

    runtime = round(time.time() - pipeline_start)

    # Count flagged signals from today's run
    signal_count = 0
    try:
        import pandas as pd
        signals_path = os.path.join(ROOT, 'data', 'processed', 'ev_signals.csv')
        if os.path.exists(signals_path):
            sig_df = pd.read_csv(signals_path)
            signal_count = int(sig_df['flag'].sum()) if 'flag' in sig_df.columns else 0
    except Exception:
        pass

    passed = sum(1 for r in results.values() if r['ok'])
    logger.log('')
    logger.log(f'Pipeline finished: {passed}/6 steps passed')
    logger.log(f'Log saved to: {LOG_FILE}')
    logger.log('=' * 50)

    # Send health summary (goes to DISCORD_WEBHOOK_HEALTH, not the bet channel)
    results_str = {
        k: v['note'] if v['ok'] else f'ERROR: {v["note"]}'
        for k, v in results.items()
    }
    try:
        from alerts.discord_alerts import send_pipeline_summary
        send_pipeline_summary(results_str, runtime_seconds=runtime, signal_count=signal_count)
    except Exception as e:
        logger.log(f'  Health summary failed: {e}')

    # Log run to Supabase
    try:
        from database import log_pipeline_run
        log_pipeline_run(
            run_date     = datetime.now().date(),
            steps_passed = passed,
            steps_failed = 6 - passed,
            notes        = ', '.join(f'{k}: {v["note"]}' for k, v in results.items() if not v['ok'])
        )
    except Exception as e:
        logger.log(f'  Supabase pipeline log error: {e}')

    logger.close()


if __name__ == '__main__':
    main()

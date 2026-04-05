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
# Discord summary message (pipeline complete notification)
# ─────────────────────────────────────────────────────────────

def send_pipeline_summary(logger, results: dict):
    try:
        from discord_webhook import DiscordWebhook, DiscordEmbed

        url = config.DISCORD_WEBHOOK_CONSERVATIVE
        if not url:
            return

        hook  = DiscordWebhook(url=url, rate_limit_retry=True)
        embed = DiscordEmbed(color=0x3498DB)

        status_lines = []
        for step, info in results.items():
            icon = 'OK' if info['ok'] else 'FAIL'
            status_lines.append(f'[{icon}]  {step}  —  {info["note"]}')

        embed.set_title('Playbook Pipeline Complete')
        embed.set_description('```\n' + '\n'.join(status_lines) + '\n```')
        embed.set_footer(text=f'Run at {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')

        hook.add_embed(embed)
        hook.execute()
    except Exception as e:
        logger.log(f'  Pipeline summary failed: {e}')


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

    results = {}

    results['Baseball Savant'] = run_step(logger, 'Step 1/6  Baseball Savant',  step_savant)
    results['FanGraphs']       = run_step(logger, 'Step 2/6  FanGraphs',         step_fangraphs)
    results['Historical Stats']= run_step(logger, 'Step 3/6  Historical Stats',  step_historical)
    results['Player Baselines']= run_step(logger, 'Step 4/6  Player Baselines',  step_baseline)
    results['Odds API']        = run_step(logger, 'Step 5/6  Odds API',           step_odds)
    results['EV Calculator']   = run_step(logger, 'Step 6/6  EV + Alerts',        step_ev)

    # Summary
    passed = sum(1 for r in results.values() if r['ok'])
    logger.log('')
    logger.log(f'Pipeline finished: {passed}/6 steps passed')
    logger.log(f'Log saved to: {LOG_FILE}')
    logger.log('=' * 50)

    send_pipeline_summary(logger, results)

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

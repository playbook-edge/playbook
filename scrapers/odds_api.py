"""
scrapers/odds_api.py

Pulls MLB pitcher strikeout props from The Odds API.

Flow:
  1. Fetch today's MLB events
  2. For each event, fetch pitcher_strikeouts market from DK / FD / BetMGM
  3. Parse each book's lines into rows (player, line, over odds, under odds)
  4. Save to data/raw/todays_props.csv

Requires ODDS_API_KEY in your .env file.
Get a free key at: https://the-odds-api.com
"""

import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import ODDS_API_KEY

RAW_DIR     = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw')
OUTPUT_PATH = os.path.join(RAW_DIR, 'todays_props.csv')
QUOTA_PATH  = os.path.join(RAW_DIR, 'odds_api_quota.txt')   # persists quota for pipeline log

BASE_URL    = 'https://api.the-odds-api.com/v4'
SPORT       = 'baseball_mlb'
BOOKMAKERS  = 'draftkings,fanduel,betmgm'
MARKETS     = 'pitcher_strikeouts'
ODDS_FORMAT = 'american'
DELAY       = 2   # seconds between event requests (API quota is per-request)


# ---------------------------------------------------------------------------
# Quota guard — fires Discord health alerts when budget is running low
# ---------------------------------------------------------------------------

def _check_quota_and_alert(resp) -> int | None:
    """
    Read x-requests-remaining from the API response header.
    Fires a Discord health alert at two thresholds:
      < 100 requests  →  warning  (consider upgrading)
      <  25 requests  →  critical (🚨 prefix)
    Returns the remaining count as an int, or None if the header is missing.
    """
    try:
        remaining = int(resp.headers.get('x-requests-remaining', -1))
    except (TypeError, ValueError):
        return None
    if remaining < 0:
        return None

    if remaining < 25:
        try:
            from alerts.discord_alerts import send_error_alert
            send_error_alert(
                'Odds API Quota',
                f'🚨 Only {remaining} requests remaining this month — consider upgrading plan'
            )
        except Exception as e:
            print(f'  Quota critical alert error: {e}')
    elif remaining < 100:
        try:
            from alerts.discord_alerts import send_error_alert
            send_error_alert(
                'Odds API Quota',
                f'Only {remaining} requests remaining this month — consider upgrading plan'
            )
        except Exception as e:
            print(f'  Quota alert error: {e}')

    return remaining


# ---------------------------------------------------------------------------
# Step 1: Get today's MLB events
# ---------------------------------------------------------------------------

def get_todays_events():
    """Fetch today's MLB events. Returns (events_list, remaining_quota)."""
    url = f'{BASE_URL}/sports/{SPORT}/events'
    params = {
        'apiKey':      ODDS_API_KEY,
        'dateFormat':  'iso',
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()

    used      = resp.headers.get('x-requests-used', 'unknown')
    remaining = _check_quota_and_alert(resp)
    print(f"  API quota — used: {used}, remaining: {remaining if remaining is not None else 'unknown'}")

    events = resp.json()

    # Filter to games starting today (in local time)
    today_str = datetime.now().strftime('%Y-%m-%d')
    todays = []
    for e in events:
        game_date = e.get('commence_time', '')[:10]  # 'YYYY-MM-DD'
        if game_date == today_str:
            todays.append(e)

    return todays, remaining


# ---------------------------------------------------------------------------
# Step 2: Fetch pitcher strikeout props for one event
# ---------------------------------------------------------------------------

def get_event_props(event_id, home_team, away_team):
    """Fetch props for one event. Returns (rows_list, remaining_quota)."""
    url = f'{BASE_URL}/sports/{SPORT}/events/{event_id}/odds'
    params = {
        'apiKey':       ODDS_API_KEY,
        'markets':      MARKETS,
        'bookmakers':   BOOKMAKERS,
        'oddsFormat':   ODDS_FORMAT,
        'dateFormat':   'iso',
    }
    resp = requests.get(url, params=params, timeout=15)

    if resp.status_code == 422:
        # Event exists but no props market available yet
        return [], None
    resp.raise_for_status()

    remaining = _check_quota_and_alert(resp)

    data = resp.json()
    rows = []

    for book in data.get('bookmakers', []):
        book_key  = book['key']
        book_name = book['title']

        for market in book.get('markets', []):
            if market['key'] != 'pitcher_strikeouts':
                continue

            # Group outcomes by player name
            # Each player has two outcomes: Over and Under
            player_lines = {}
            for outcome in market.get('outcomes', []):
                player = outcome.get('description', '')   # pitcher name
                side   = outcome.get('name', '')          # 'Over' or 'Under'
                line   = outcome.get('point')             # e.g. 5.5
                odds   = outcome.get('price')             # American odds e.g. -115

                if player not in player_lines:
                    player_lines[player] = {
                        'player':     player,
                        'matchup':    f'{away_team} @ {home_team}',
                        'prop_type':  'pitcher_strikeouts',
                        'line':       line,
                        'over_odds':  None,
                        'under_odds': None,
                        'book':       book_name,
                        'book_key':   book_key,
                    }

                if side == 'Over':
                    player_lines[player]['over_odds'] = odds
                    player_lines[player]['line']      = line
                elif side == 'Under':
                    player_lines[player]['under_odds'] = odds

            rows.extend(player_lines.values())

    return rows, remaining


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    print('=' * 55)
    print('  PLAYBOOK -- Odds API Scraper (Pitcher Props)')
    print('=' * 55)

    if not ODDS_API_KEY or ODDS_API_KEY == 'your_odds_api_key_here':
        print('\nERROR: ODDS_API_KEY is not set.')
        print('  1. Go to https://the-odds-api.com and get a free key')
        print('  2. Create a .env file in the project root')
        print('  3. Add: ODDS_API_KEY=your_key_here')
        return

    os.makedirs(RAW_DIR, exist_ok=True)

    # 1. Today's events
    print('\nFetching today\'s MLB events...')
    events, quota_remaining = get_todays_events()
    print(f'  Found {len(events)} games today.')

    if not events:
        print('No games today. Nothing to scrape.')
        return

    # 2. Props for each event
    all_rows = []
    target_books = set(BOOKMAKERS.split(','))

    for i, event in enumerate(events, 1):
        home  = event.get('home_team', '')
        away  = event.get('away_team', '')
        eid   = event['id']
        print(f'  [{i}/{len(events)}] {away} @ {home}')

        rows, evt_quota = get_event_props(eid, home, away)
        if evt_quota is not None:
            quota_remaining = evt_quota   # keep updating to track most recent
        all_rows.extend(rows)
        time.sleep(DELAY)

    # Write final quota to file so the pipeline logger can include it
    if quota_remaining is not None:
        try:
            os.makedirs(RAW_DIR, exist_ok=True)
            with open(QUOTA_PATH, 'w') as _qf:
                _qf.write(str(quota_remaining))
            print(f'\n  Final Odds API quota: {quota_remaining} requests remaining this month')
        except Exception as _e:
            print(f'  Could not write quota file: {_e}')

    # 3. Build dataframe
    if not all_rows:
        print('\nNo pitcher strikeout props found across any books.')
        print('This can happen if:')
        print('  - Props haven\'t been posted yet (usually go up morning of game day)')
        print('  - Your API plan doesn\'t include player props')
        return

    df = pd.DataFrame(all_rows)

    # Clean up columns
    df = df[[
        'player', 'matchup', 'prop_type', 'line',
        'over_odds', 'under_odds', 'book'
    ]]
    df = df.sort_values(['player', 'book']).reset_index(drop=True)

    # 4. Save
    df.to_csv(OUTPUT_PATH, index=False)
    print(f'\nSaved to: {os.path.normpath(OUTPUT_PATH)}')

    # 5. Summary
    books_found   = df['book'].nunique()
    players_found = df['player'].nunique()
    total_lines   = len(df)

    print(f'\n{"=" * 55}')
    print(f'  Summary')
    print(f'{"=" * 55}')
    print(f'  Total prop lines:  {total_lines}')
    print(f'  Unique pitchers:   {players_found}')
    print(f'  Books with data:   {books_found}')
    print(f'  Books:             {", ".join(sorted(df["book"].unique()))}')

    print(f'\n--- Props Preview ---')
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 120)
    print(df.head(15).to_string(index=False))

    # Line comparison across books (where multiple books have the same player)
    print(f'\n--- Line Shopping: Where Books Disagree ---')
    pivot = df.pivot_table(
        index='player', columns='book', values='line', aggfunc='first'
    )
    # Only show players where books disagree on the line
    if pivot.shape[1] > 1:
        disagree = pivot[pivot.nunique(axis=1) > 1]
        if not disagree.empty:
            print(disagree.to_string())
        else:
            print('  All books agree on lines for every pitcher.')
    else:
        print('  Only one book returned data — line shopping requires 2+ books.')


if __name__ == '__main__':
    run()

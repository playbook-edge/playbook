"""
scrapers/weather_scraper.py — Game-day weather conditions for MLB stadiums.

Uses Open-Meteo API (free, no key required):
  https://api.open-meteo.com/v1/forecast

Runs as Step 2.6 in main.py (after umpire_scraper, before historical_stats).
Output: data/raw/weather_today.csv

Columns:
  home_team, stadium, temperature_f, wind_speed_mph, wind_direction_deg,
  wind_label, wind_factor, precip_pct, is_dome
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

RAW              = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw')
OPEN_METEO_URL   = 'https://api.open-meteo.com/v1/forecast'
MLB_SCHEDULE_URL = 'https://statsapi.mlb.com/api/v1/schedule'


# ── Part 1 — Stadium coordinates ────────────────────────────────────────────
#
# cf_degrees: compass direction FROM home plate TOWARD center field.
#   0 = North  |  90 = East  |  180 = South  |  270 = West
#
# dome: True for enclosed or retractable-roof stadiums.
#   Dome games get no weather adjustment regardless of roof position.

STADIUM_COORDS = {
    # ── Outdoor stadiums ──────────────────────────────────────────────────────
    'ATL': {'name': 'Truist Park',                 'lat': 33.8908,  'lon': -84.4678,  'cf_degrees': 25,  'dome': False},
    'BAL': {'name': 'Oriole Park at Camden Yards', 'lat': 39.2838,  'lon': -76.6218,  'cf_degrees': 5,   'dome': False},
    'BOS': {'name': 'Fenway Park',                 'lat': 42.3467,  'lon': -71.0972,  'cf_degrees': 60,  'dome': False},
    'CHC': {'name': 'Wrigley Field',               'lat': 41.9484,  'lon': -87.6553,  'cf_degrees': 30,  'dome': False},
    'CWS': {'name': 'Guaranteed Rate Field',       'lat': 41.8299,  'lon': -87.6338,  'cf_degrees': 315, 'dome': False},
    'CIN': {'name': 'Great American Ball Park',    'lat': 39.0979,  'lon': -84.5082,  'cf_degrees': 340, 'dome': False},
    'CLE': {'name': 'Progressive Field',           'lat': 41.4962,  'lon': -81.6852,  'cf_degrees': 345, 'dome': False},
    'COL': {'name': 'Coors Field',                 'lat': 39.7559,  'lon': -104.9942, 'cf_degrees': 40,  'dome': False},
    'DET': {'name': 'Comerica Park',               'lat': 42.3390,  'lon': -83.0485,  'cf_degrees': 355, 'dome': False},
    'KC':  {'name': 'Kauffman Stadium',            'lat': 39.0517,  'lon': -94.4803,  'cf_degrees': 330, 'dome': False},
    'LAA': {'name': 'Angel Stadium',               'lat': 33.8003,  'lon': -117.8827, 'cf_degrees': 290, 'dome': False},
    'LAD': {'name': 'Dodger Stadium',              'lat': 34.0739,  'lon': -118.2400, 'cf_degrees': 330, 'dome': False},
    'MIN': {'name': 'Target Field',                'lat': 44.9817,  'lon': -93.2781,  'cf_degrees': 330, 'dome': False},
    'NYM': {'name': 'Citi Field',                  'lat': 40.7571,  'lon': -73.8458,  'cf_degrees': 325, 'dome': False},
    'NYY': {'name': 'Yankee Stadium',              'lat': 40.8296,  'lon': -73.9262,  'cf_degrees': 20,  'dome': False},
    'ATH': {'name': 'Sutter Health Park',          'lat': 38.5733,  'lon': -121.5086, 'cf_degrees': 0,   'dome': False},
    'PHI': {'name': 'Citizens Bank Park',          'lat': 39.9061,  'lon': -75.1665,  'cf_degrees': 15,  'dome': False},
    'PIT': {'name': 'PNC Park',                    'lat': 40.4468,  'lon': -80.0057,  'cf_degrees': 325, 'dome': False},
    'SD':  {'name': 'Petco Park',                  'lat': 32.7073,  'lon': -117.1568, 'cf_degrees': 310, 'dome': False},
    'SF':  {'name': 'Oracle Park',                 'lat': 37.7786,  'lon': -122.3893, 'cf_degrees': 345, 'dome': False},
    'STL': {'name': 'Busch Stadium',               'lat': 38.6226,  'lon': -90.1928,  'cf_degrees': 20,  'dome': False},
    'WSH': {'name': 'Nationals Park',              'lat': 38.8730,  'lon': -77.0074,  'cf_degrees': 45,  'dome': False},
    # ── Dome / retractable-roof stadiums ─────────────────────────────────────
    'AZ':  {'name': 'Chase Field',                 'lat': 33.4455,  'lon': -112.0667, 'cf_degrees': 345, 'dome': True},
    'HOU': {'name': 'Minute Maid Park',            'lat': 29.7572,  'lon': -95.3555,  'cf_degrees': 0,   'dome': True},
    'MIA': {'name': 'loanDepot park',              'lat': 25.7781,  'lon': -80.2197,  'cf_degrees': 0,   'dome': True},
    'MIL': {'name': 'American Family Field',       'lat': 43.0280,  'lon': -87.9712,  'cf_degrees': 350, 'dome': True},
    'SEA': {'name': 'T-Mobile Park',               'lat': 47.5914,  'lon': -122.3326, 'cf_degrees': 0,   'dome': True},
    'TB':  {'name': 'Tropicana Field',             'lat': 27.7682,  'lon': -82.6534,  'cf_degrees': 0,   'dome': True},
    'TEX': {'name': 'Globe Life Field',            'lat': 32.7512,  'lon': -97.0832,  'cf_degrees': 0,   'dome': True},
    'TOR': {'name': 'Rogers Centre',               'lat': 43.6414,  'lon': -79.3894,  'cf_degrees': 0,   'dome': True},
}

# All the ways a team can be named → STADIUM_COORDS key
NAME_TO_CODE = {
    # Full names
    'arizona diamondbacks': 'AZ',   'atlanta braves': 'ATL',
    'baltimore orioles': 'BAL',     'boston red sox': 'BOS',
    'chicago cubs': 'CHC',          'chicago white sox': 'CWS',
    'cincinnati reds': 'CIN',       'cleveland guardians': 'CLE',
    'colorado rockies': 'COL',      'detroit tigers': 'DET',
    'houston astros': 'HOU',        'kansas city royals': 'KC',
    'los angeles angels': 'LAA',    'los angeles dodgers': 'LAD',
    'miami marlins': 'MIA',         'milwaukee brewers': 'MIL',
    'minnesota twins': 'MIN',       'new york mets': 'NYM',
    'new york yankees': 'NYY',      'oakland athletics': 'ATH',
    'philadelphia phillies': 'PHI', 'pittsburgh pirates': 'PIT',
    'san diego padres': 'SD',       'san francisco giants': 'SF',
    'seattle mariners': 'SEA',      'st. louis cardinals': 'STL',
    'tampa bay rays': 'TB',         'texas rangers': 'TEX',
    'toronto blue jays': 'TOR',     'washington nationals': 'WSH',
    # Abbreviations (MLB API + FanGraphs variants)
    'ari': 'AZ',  'atl': 'ATL', 'bal': 'BAL', 'bos': 'BOS',
    'chc': 'CHC', 'cws': 'CWS', 'cin': 'CIN', 'cle': 'CLE',
    'col': 'COL', 'det': 'DET', 'hou': 'HOU', 'kc':  'KC',
    'laa': 'LAA', 'lad': 'LAD', 'mia': 'MIA', 'mil': 'MIL',
    'min': 'MIN', 'nym': 'NYM', 'nyy': 'NYY', 'oak': 'ATH',
    'phi': 'PHI', 'pit': 'PIT', 'sd':  'SD',  'sf':  'SF',
    'sea': 'SEA', 'stl': 'STL', 'tb':  'TB',  'tex': 'TEX',
    'tor': 'TOR', 'wsh': 'WSH', 'was': 'WSH', 'az':  'AZ',
    # Nicknames
    'diamondbacks': 'AZ',   'braves': 'ATL',    'orioles': 'BAL',
    'red sox': 'BOS',       'cubs': 'CHC',      'white sox': 'CWS',
    'reds': 'CIN',          'guardians': 'CLE', 'rockies': 'COL',
    'tigers': 'DET',        'astros': 'HOU',    'royals': 'KC',
    'angels': 'LAA',        'dodgers': 'LAD',   'marlins': 'MIA',
    'brewers': 'MIL',       'twins': 'MIN',     'mets': 'NYM',
    'yankees': 'NYY',       'athletics': 'ATH', 'phillies': 'PHI',
    'pirates': 'PIT',       'padres': 'SD',     'giants': 'SF',
    'mariners': 'SEA',      'cardinals': 'STL', 'rays': 'TB',
    'rangers': 'TEX',       'blue jays': 'TOR', 'nationals': 'WSH',
}


def _team_to_code(name: str) -> str | None:
    """Convert any team name / abbreviation to a STADIUM_COORDS key."""
    key = str(name).lower().strip()
    if key in NAME_TO_CODE:
        return NAME_TO_CODE[key]
    # Try each word for nicknames that appear in longer strings
    for word in key.split():
        if word in NAME_TO_CODE:
            return NAME_TO_CODE[word]
    return None


def _angle_diff(a: float, b: float) -> float:
    """Smallest angle between two compass bearings (result 0–180)."""
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff


def _degrees_to_cardinal(deg: float) -> str:
    """Convert a compass bearing to an 8-point cardinal label."""
    dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
    return dirs[round(deg / 45) % 8]


# ── Part 2 — Fetch forecast ──────────────────────────────────────────────────

def get_game_weather(home_team_code: str,
                     game_datetime_utc: datetime) -> dict | None:
    """
    Fetch Open-Meteo hourly forecast for a stadium at game time.

    Returns a dict with keys:
      temperature_f, wind_speed_mph, wind_direction_deg, precip_pct, is_dome

    Returns None if the team code is not found in STADIUM_COORDS.
    Dome stadiums return immediately with is_dome=True and zero wind values.
    """
    code = str(home_team_code).upper()
    info = STADIUM_COORDS.get(code)
    if not info:
        return None

    if info['dome']:
        return {
            'temperature_f':    None,
            'wind_speed_mph':   0.0,
            'wind_direction_deg': 0.0,
            'precip_pct':       0,
            'is_dome':          True,
        }

    game_date = game_datetime_utc.strftime('%Y-%m-%d')

    params = {
        'latitude':    info['lat'],
        'longitude':   info['lon'],
        'hourly':      'windspeed_10m,winddirection_10m,temperature_2m,precipitation_probability',
        'wind_speed_unit':   'mph',
        'temperature_unit':  'fahrenheit',
        'timezone':          'UTC',
        'start_date':        game_date,
        'end_date':          game_date,
    }

    resp = requests.get(OPEN_METEO_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    hourly = data.get('hourly', {})
    times  = hourly.get('time', [])
    if not times:
        return None

    # Find the hour closest to first pitch (UTC)
    target = game_datetime_utc.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    best_idx, best_diff = 0, float('inf')
    for i, t in enumerate(times):
        t_dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        diff = abs((t_dt - target).total_seconds())
        if diff < best_diff:
            best_diff, best_idx = diff, i

    return {
        'temperature_f':      hourly['temperature_2m'][best_idx],
        'wind_speed_mph':     hourly['windspeed_10m'][best_idx],
        'wind_direction_deg': hourly['winddirection_10m'][best_idx],
        'precip_pct':         hourly['precipitation_probability'][best_idx],
        'is_dome':            False,
    }


# ── Part 3 — Wind adjustment ─────────────────────────────────────────────────

def calculate_wind_adjustment(wind_speed_mph: float,
                               wind_direction_deg: float,
                               cf_direction_deg: float,
                               is_dome: bool) -> tuple[float, str]:
    """
    Calculate whether wind is helping or hurting hitters.

    Wind direction follows meteorological convention: the direction wind is
    COMING FROM (e.g., 180° = southerly wind blowing northward).

    Returns (wind_factor, wind_label).

    wind_factor:
      +0.25  out 15+ mph      (wind blowing toward CF — ball carries)
      +0.15  out 10-15 mph
       0.00  across or < 10 mph
      -0.15  in 10-15 mph    (wind blowing toward home — ball dies)
      -0.25  in 15+ mph

    wind_label examples: "18mph out to CF", "12mph in from CF",
                         "8mph across (NW)", "Calm", "Dome"
    """
    if is_dome:
        return 0.0, 'Dome'

    if wind_speed_mph < 3:
        return 0.0, 'Calm'

    # "Out to CF": wind comes FROM the direction opposite CF, pushes ball toward CF.
    #   e.g., CF at 0° (N) → wind from 180° (S) blows ball north → out
    # "In from CF": wind comes FROM the CF direction, pushes ball toward home plate.
    #   e.g., CF at 0° (N) → wind from 0° (N) blows ball south → in
    out_source = (cf_direction_deg + 180) % 360   # wind must come from here to blow out
    diff_out   = _angle_diff(wind_direction_deg, out_source)
    diff_in    = _angle_diff(wind_direction_deg, cf_direction_deg)

    if diff_out <= 45:
        direction = 'out'
    elif diff_in <= 45:
        direction = 'in'
    else:
        direction = 'across'

    # Factor
    if direction == 'out':
        factor = 0.25 if wind_speed_mph >= 15 else 0.15
    elif direction == 'in':
        factor = -0.25 if wind_speed_mph >= 15 else -0.15
    else:
        factor = 0.0

    # If wind is under 10mph, no meaningful factor even for out/in
    if wind_speed_mph < 10:
        factor = 0.0

    # Label
    cardinal = _degrees_to_cardinal(wind_direction_deg)
    if direction == 'out':
        label = f'{wind_speed_mph:.0f}mph out to CF'
    elif direction == 'in':
        label = f'{wind_speed_mph:.0f}mph in from CF'
    else:
        label = f'{wind_speed_mph:.0f}mph across ({cardinal})'

    return round(factor, 2), label


# ── Part 4 — Wire into pipeline ──────────────────────────────────────────────

def get_todays_weather(date_str: str | None = None) -> pd.DataFrame:
    """
    Fetch weather for every home stadium playing today.

    Calls the MLB Stats API for today's schedule, then Open-Meteo for each
    outdoor stadium. Returns a DataFrame and saves to data/raw/weather_today.csv.

    Degrades gracefully: if a single stadium call fails, that row is skipped
    and the pipeline continues.
    """
    today = date_str or datetime.now().strftime('%Y-%m-%d')
    print(f'  Fetching MLB schedule for {today}...')

    try:
        resp = requests.get(
            MLB_SCHEDULE_URL,
            params={'sportId': 1, 'date': today},
            timeout=10,
        )
        resp.raise_for_status()
        schedule_data = resp.json()
    except Exception as e:
        print(f'  MLB schedule fetch failed: {e}')
        return pd.DataFrame()

    dates = schedule_data.get('dates', [])
    if not dates:
        print('  No games found today.')
        return pd.DataFrame()

    games = dates[0].get('games', [])
    print(f'  Found {len(games)} game(s).')

    rows = []
    for game in games:
        home_name = game.get('teams', {}).get('home', {}).get('team', {}).get('name', '')
        game_date_str = game.get('gameDate', '')  # "2026-04-06T23:05:00Z"

        code = _team_to_code(home_name)
        if not code:
            print(f'  [skip] unknown home team: {home_name}')
            continue

        info = STADIUM_COORDS.get(code)
        if not info:
            print(f'  [skip] no stadium entry for {code}')
            continue

        # Parse game time (UTC)
        try:
            game_dt = datetime.fromisoformat(
                game_date_str.replace('Z', '+00:00')
            ).replace(tzinfo=timezone.utc)
        except Exception:
            # Default to 7 PM ET = 23:00 UTC if time is missing
            game_dt = datetime.strptime(today, '%Y-%m-%d').replace(
                hour=23, minute=0, tzinfo=timezone.utc
            )

        # Fetch weather
        try:
            wx = get_game_weather(code, game_dt)
            time.sleep(0.3)   # be gentle on the free API
        except Exception as e:
            print(f'  [skip] weather fetch failed for {home_name} ({code}): {e}')
            continue

        if wx is None:
            continue

        wind_factor, wind_label = calculate_wind_adjustment(
            wx['wind_speed_mph'],
            wx['wind_direction_deg'],
            info['cf_degrees'],
            wx['is_dome'],
        )

        temp_str = f"{wx['temperature_f']:.0f}" if wx['temperature_f'] is not None else 'N/A'
        status   = 'Dome' if wx['is_dome'] else f"{wind_label}, {temp_str}F, precip {wx['precip_pct']}%"
        print(f'  {code:<4} {info["name"]:<30} {status}  factor={wind_factor:+.2f}')

        rows.append({
            'home_team':         code,
            'stadium':           info['name'],
            'temperature_f':     wx['temperature_f'],
            'wind_speed_mph':    wx['wind_speed_mph'],
            'wind_direction_deg': wx['wind_direction_deg'],
            'wind_label':        wind_label,
            'wind_factor':       wind_factor,
            'precip_pct':        wx['precip_pct'],
            'is_dome':           wx['is_dome'],
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        os.makedirs(RAW, exist_ok=True)
        out_path = os.path.join(RAW, 'weather_today.csv')
        df.to_csv(out_path, index=False)
        print(f'  Weather saved: {len(df)} stadiums -- {os.path.normpath(out_path)}')
    return df


def run():
    print('=' * 60)
    print('  PLAYBOOK — Weather Scraper')
    print('=' * 60)
    df = get_todays_weather()
    if df.empty:
        print('  No weather data generated.')
        return
    print()
    print(df[['home_team', 'stadium', 'temperature_f', 'wind_label',
              'wind_factor', 'precip_pct', 'is_dome']].to_string(index=False))


if __name__ == '__main__':
    run()

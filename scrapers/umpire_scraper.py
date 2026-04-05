"""
scrapers/umpire_scraper.py

Two jobs:

Part 1 — Build umpire profiles (weekly refresh)
    Fetches historical umpire tendency data from Baseball Savant's umpire
    scorecard endpoint.  Computes and stores in Supabase:
        umpire_name, umpire_id, zone_size_pct (0-100 percentile rank),
        k_per_game, runs_per_game, first_pitch_strike_pct, last_updated.
    Refreshed weekly — historical tendencies don't move game-to-game.

Part 2 — Today's umpire assignments
    Calls the MLB Stats API with hydrate=officials to find the home plate
    umpire for each game.  Matches to umpire_profiles by umpire ID.
    Returns a dict keyed by normalised matchup string so ev_calculator
    can look up adjustments by the same matchup field it already tracks.
"""

import os
import sys
import time
import requests
import pandas as pd
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

TODAY        = datetime.now().strftime('%Y-%m-%d')
CURRENT_YEAR = datetime.now().year
REFRESH_DAYS = 7          # rebuild profiles at most once per week
DELAY        = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    if not isinstance(name, str):
        return ''
    return name.lower().strip()


def _normalize_matchup(away: str, home: str) -> str:
    """'Pittsburgh Pirates @ San Francisco Giants' style key, lowercased."""
    return f"{away.lower().strip()} @ {home.lower().strip()}"


# ---------------------------------------------------------------------------
# Part 1A — fetch raw umpire stats from umpscorecards.com
# ---------------------------------------------------------------------------
#
# umpscorecards.com provides a clean public JSON API for umpire tendency data.
#
# Key field used for zone_size_pct:
#   total_run_impact_mean — average run impact per game from missed calls.
#   Lower value = fewer runs from missed calls = pitcher-friendly zone.
#   We INVERT the percentile rank so high zone_size_pct = more pitcher-friendly.
#   Example: Edwin Jimenez (0.915 run_impact) → ~95th pct → +3% K boost.
#            Marcus Pattillo (2.382 run_impact) → ~5th pct → -3% K trim.
#
# Note: umpscorecards does not expose per-game K or first-pitch-strike rates
# via the summary endpoint.  Those columns are stored as None in profiles.
# ---------------------------------------------------------------------------

UMPSCORECARDS_URL = 'https://umpscorecards.com/api/umpires'


def _fetch_umpscorecards(year: int) -> list:
    """
    Pull umpire stats from umpscorecards.com for `year`.
    Falls back to all-time data (no season filter) if year returns no rows.
    Returns a list of raw row dicts, or [] on failure.
    """
    for url in [
        f'{UMPSCORECARDS_URL}?season={year}',
        UMPSCORECARDS_URL,                      # all-time fallback
    ]:
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            rows = resp.json().get('rows', [])
            if rows:
                print(f'  umpscorecards.com: {len(rows)} umpires '
                      f'({url.split("?")[1] if "?" in url else "all-time"}).')
                return rows
        except Exception as e:
            print(f'  umpscorecards fetch failed ({url}): {e}')

    return []


def _compute_profiles(raw: list) -> list:
    """
    Normalise raw umpscorecards rows into clean profile dicts.

    zone_size_pct (0-100):
        Percentile rank of how pitcher-friendly this umpire's zone is.
        Derived by INVERTING the rank of total_run_impact_mean.
        100th pct = fewest runs from missed calls = tightest zone for batters.
        0th pct   = most runs from missed calls   = largest zone for batters.

    Matching in get_todays_umpires() is done by normalised full name since
    umpscorecards does not expose MLB umpire IDs.
    """
    if not raw:
        return []

    df = pd.DataFrame(raw)

    # zone_size_pct: invert rank of total_run_impact_mean
    # (lower run_impact = more pitcher-friendly = higher pct)
    if 'total_run_impact_mean' in df.columns:
        df['total_run_impact_mean'] = pd.to_numeric(
            df['total_run_impact_mean'], errors='coerce')
        df['zone_size_pct'] = (
            df['total_run_impact_mean']
            .rank(pct=True, ascending=True)   # ascending: low impact → low rank
            .rsub(1)                           # invert: low impact → high pct
            .mul(100).round(0)
            .astype('Int64')
        )
    else:
        df['zone_size_pct'] = 50

    profiles = []
    for _, r in df.iterrows():
        name = str(r.get('umpire', '')).strip()
        if not name:
            continue

        def _f(col):
            v = r.get(col)
            try:
                f = float(v)
                return round(f, 3) if f == f else None
            except (TypeError, ValueError):
                return None

        profiles.append({
            'umpire_name':            name,
            'umpire_id':              None,   # not available from umpscorecards
            'zone_size_pct':          int(r.get('zone_size_pct', 50)),
            'k_per_game':             None,   # not in summary endpoint
            'runs_per_game':          _f('total_run_impact_mean'),
            'first_pitch_strike_pct': None,   # not in summary endpoint
            'last_updated':           TODAY,
        })

    return profiles


# ---------------------------------------------------------------------------
# Part 1B — weekly-cached profile build
# ---------------------------------------------------------------------------

def build_umpire_profiles(force: bool = False) -> list:
    """
    Return umpire profiles.  Reads from Supabase cache; only re-fetches
    from Baseball Savant if the cache is older than REFRESH_DAYS.

    force=True bypasses the weekly check (useful for manual refresh).
    Returns a list of profile dicts, or [] if unavailable.
    """
    # ── Check weekly cache ────────────────────────────────────────────────
    if not force:
        try:
            from database import get_umpire_profiles, get_umpire_last_updated
            last_updated = get_umpire_last_updated()
            if last_updated:
                age = (date.today() - date.fromisoformat(str(last_updated)[:10])).days
                if age < REFRESH_DAYS:
                    cached = get_umpire_profiles()
                    if cached:
                        print(f'  Umpire profiles: {len(cached)} umpires from cache '
                              f'(updated {age}d ago).')
                        return cached
        except Exception as e:
            print(f'  Umpire cache check failed ({e}) — fetching fresh.')

    # ── Fresh fetch ───────────────────────────────────────────────────────
    print(f'  Fetching umpire profiles from umpscorecards.com ({CURRENT_YEAR})...')
    time.sleep(DELAY)
    raw      = _fetch_umpscorecards(CURRENT_YEAR)
    profiles = _compute_profiles(raw)

    if not profiles:
        print('  No umpire profiles built — Baseball Savant unavailable.')
        return []

    # ── Save to Supabase ──────────────────────────────────────────────────
    try:
        from database import save_umpire_profiles
        saved = save_umpire_profiles(profiles)
        print(f'  Saved {saved} umpire profiles to Supabase.')
    except Exception as e:
        print(f'  Supabase umpire save failed ({e}) — using in-memory profiles.')

    return profiles


# ---------------------------------------------------------------------------
# Part 2 — today's umpire assignments
# ---------------------------------------------------------------------------

def get_todays_umpires(profiles: list | None = None) -> dict:
    """
    Fetch today's home plate umpire assignments from the MLB Stats API
    and match each umpire to their profile data.

    Returns:
        {
            'pittsburgh pirates @ san francisco giants': {
                'umpire_name':       'Joe West',
                'umpire_id':         '427474',
                'zone_size_pct':     72,
                'k_per_game':        15.3,
                'runs_per_game':     8.8,
                'k_factor':          1.03,   # applied to K probability
                'note':              'tight zone (72nd pct)',
            },
            ...
        }
    Key is normalised '{away} @ {home}' string, matching the matchup column
    in ev_signals.csv.
    """
    if profiles is None:
        profiles = build_umpire_profiles()

    # umpscorecards does not provide MLB umpire IDs, so we match by name only.
    # The by_name dict uses normalised full name as key.
    by_name = {_normalize_name(p['umpire_name']): p for p in profiles}

    result = {}
    try:
        url  = (
            f'https://statsapi.mlb.com/api/v1/schedule'
            f'?sportId=1&date={TODAY}&hydrate=officials'
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        games = resp.json().get('dates', [{}])[0].get('games', [])

        for game in games:
            home = game.get('teams', {}).get('home', {}).get('team', {}).get('name', '')
            away = game.get('teams', {}).get('away', {}).get('team', {}).get('name', '')
            if not home or not away:
                continue

            matchup_key = _normalize_matchup(away, home)

            # Find home plate umpire from officials list
            hp_umpire = None
            for official in game.get('officials', []):
                if official.get('officialType', '').lower() == 'home plate':
                    hp_umpire = official.get('official', {})
                    break

            if not hp_umpire:
                result[matchup_key] = {'umpire_name': None, 'umpire_id': None,
                                       'k_factor': 1.0, 'note': 'no umpire data'}
                continue

            uid   = str(hp_umpire.get('id', ''))
            uname = hp_umpire.get('fullName', '')

            # Match to profile by full name (umpscorecards has no MLB IDs)
            profile = by_name.get(_normalize_name(uname))

            if not profile:
                result[matchup_key] = {
                    'umpire_name':    uname,
                    'umpire_id':      uid,
                    'zone_size_pct':  50,
                    'k_per_game':     None,
                    'runs_per_game':  None,
                    'k_factor':       1.0,
                    'note':           'umpire assigned but no profile data',
                }
                continue

            zone_pct = int(profile.get('zone_size_pct', 50))

            # K adjustment: tight zone (>60th pct) boosts Ks; large zone (<40th) trims
            if zone_pct > 60:
                k_factor = 1.03
                note     = f'tight zone ({zone_pct}th pct) +3%'
            elif zone_pct < 40:
                k_factor = 0.97
                note     = f'large zone ({zone_pct}th pct) -3%'
            else:
                k_factor = 1.0
                note     = f'neutral zone ({zone_pct}th pct)'

            result[matchup_key] = {
                'umpire_name':    uname,
                'umpire_id':      uid,
                'zone_size_pct':  zone_pct,
                'k_per_game':     profile.get('k_per_game'),
                'runs_per_game':  profile.get('runs_per_game'),
                'k_factor':       k_factor,
                'note':           note,
            }

    except Exception as e:
        print(f'  Umpire assignment fetch failed: {e}')

    return result


# ---------------------------------------------------------------------------
# Standalone run
# ---------------------------------------------------------------------------

def run():
    print('=' * 55)
    print('  PLAYBOOK -- Umpire Scraper')
    print('=' * 55)

    print('\nPart 1: Building umpire profiles...')
    profiles = build_umpire_profiles()
    if profiles:
        df = pd.DataFrame(profiles)
        print(f'\n  {len(profiles)} umpire profiles built.')
        print(df[['umpire_name', 'zone_size_pct', 'k_per_game',
                  'runs_per_game']].head(10).to_string(index=False))

    print('\nPart 2: Today\'s umpire assignments...')
    assignments = get_todays_umpires(profiles)
    if assignments:
        print(f'\n  {len(assignments)} game(s) today:\n')
        for matchup, info in assignments.items():
            name    = info.get('umpire_name') or 'Unknown'
            zone    = info.get('zone_size_pct', '?')
            factor  = info.get('k_factor', 1.0)
            note    = info.get('note', '')
            print(f'  {matchup}')
            print(f'    HP Umpire: {name}  |  Zone: {zone}th pct  |  '
                  f'K factor: {factor:+.0%}  |  {note}')
    else:
        print('  No games today or umpire assignments unavailable.')


if __name__ == '__main__':
    run()

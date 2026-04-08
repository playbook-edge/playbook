"""
scrapers/park_factors.py

Ballpark context for all 30 MLB stadiums.

Two factors per park:
  general_factor  — 3-year average park factor for runs (2022-2024), centered at 100.
                    Above 100 = hitter-friendly, below 100 = pitcher-friendly.
  k_factor        — strikeout-specific park factor, centered at 100.
                    Above 100 = more Ks happen here, below 100 = fewer.

Sources: Baseball Reference Park Factors (2022-2024 averages), FanGraphs
park factor leaderboard, and SABR park factor research.

Keys match the full team names in savant_today.csv / matchup strings.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Part 1 — Park factors dictionary (all 30 MLB stadiums)
# ──────────────────────────────────────────────────────────────────────────────

PARK_FACTORS: dict[str, dict] = {
    'Arizona Diamondbacks': {
        'park_name':      'Chase Field',
        'general_factor': 101,
        'k_factor':       102,
        'dome':           True,   # retractable roof, usually closed
        'altitude_ft':    1082,
    },
    'Atlanta Braves': {
        'park_name':      'Truist Park',
        'general_factor': 101,
        'k_factor':       101,
        'dome':           False,
        'altitude_ft':    1050,
    },
    'Athletics': {
        'park_name':      'Sutter Health Park',   # Sacramento (2025 temp home)
        'general_factor': 100,
        'k_factor':       100,
        'dome':           False,
        'altitude_ft':    25,
    },
    'Baltimore Orioles': {
        'park_name':      'Oriole Park at Camden Yards',
        'general_factor': 101,
        'k_factor':       99,
        'dome':           False,
        'altitude_ft':    20,
    },
    'Boston Red Sox': {
        'park_name':      'Fenway Park',
        'general_factor': 104,
        'k_factor':       95,    # short dimensions, lots of contact play
        'dome':           False,
        'altitude_ft':    20,
    },
    'Chicago Cubs': {
        'park_name':      'Wrigley Field',
        'general_factor': 103,
        'k_factor':       97,    # wind and ivy suppress Ks
        'dome':           False,
        'altitude_ft':    595,
    },
    'Chicago White Sox': {
        'park_name':      'Guaranteed Rate Field',
        'general_factor': 101,
        'k_factor':       101,
        'dome':           False,
        'altitude_ft':    595,
    },
    'Cincinnati Reds': {
        'park_name':      'Great American Ball Park',
        'general_factor': 106,
        'k_factor':       103,
        'dome':           False,
        'altitude_ft':    490,
    },
    'Cleveland Guardians': {
        'park_name':      'Progressive Field',
        'general_factor': 98,
        'k_factor':       99,
        'dome':           False,
        'altitude_ft':    653,
    },
    'Colorado Rockies': {
        'park_name':      'Coors Field',
        'general_factor': 115,
        'k_factor':       88,    # thin air = less movement = harder to miss
        'dome':           False,
        'altitude_ft':    5280,
    },
    'Detroit Tigers': {
        'park_name':      'Comerica Park',
        'general_factor': 97,
        'k_factor':       98,
        'dome':           False,
        'altitude_ft':    600,
    },
    'Houston Astros': {
        'park_name':      'Minute Maid Park',
        'general_factor': 99,
        'k_factor':       101,
        'dome':           True,   # retractable roof
        'altitude_ft':    43,
    },
    'Kansas City Royals': {
        'park_name':      'Kauffman Stadium',
        'general_factor': 99,
        'k_factor':       98,
        'dome':           False,
        'altitude_ft':    909,
    },
    'Los Angeles Angels': {
        'park_name':      'Angel Stadium',
        'general_factor': 98,
        'k_factor':       99,
        'dome':           False,
        'altitude_ft':    160,
    },
    'Los Angeles Dodgers': {
        'park_name':      'Dodger Stadium',
        'general_factor': 97,
        'k_factor':       100,
        'dome':           False,
        'altitude_ft':    512,
    },
    'Miami Marlins': {
        'park_name':      'LoanDepot Park',
        'general_factor': 94,
        'k_factor':       96,    # large park, pitcher-friendly
        'dome':           True,
        'altitude_ft':    6,
    },
    'Milwaukee Brewers': {
        'park_name':      'American Family Field',
        'general_factor': 100,
        'k_factor':       101,
        'dome':           True,   # retractable roof
        'altitude_ft':    635,
    },
    'Minnesota Twins': {
        'park_name':      'Target Field',
        'general_factor': 99,
        'k_factor':       100,
        'dome':           False,
        'altitude_ft':    830,
    },
    'New York Mets': {
        'park_name':      'Citi Field',
        'general_factor': 97,
        'k_factor':       101,
        'dome':           False,
        'altitude_ft':    20,
    },
    'New York Yankees': {
        'park_name':      'Yankee Stadium',
        'general_factor': 104,
        'k_factor':       100,
        'dome':           False,
        'altitude_ft':    55,
    },
    'Philadelphia Phillies': {
        'park_name':      'Citizens Bank Park',
        'general_factor': 105,
        'k_factor':       102,
        'dome':           False,
        'altitude_ft':    20,
    },
    'Pittsburgh Pirates': {
        'park_name':      'PNC Park',
        'general_factor': 97,
        'k_factor':       98,
        'dome':           False,
        'altitude_ft':    730,
    },
    'San Diego Padres': {
        'park_name':      'Petco Park',
        'general_factor': 93,
        'k_factor':       97,    # marine layer suppresses offense and Ks
        'dome':           False,
        'altitude_ft':    20,
    },
    'San Francisco Giants': {
        'park_name':      'Oracle Park',
        'general_factor': 93,
        'k_factor':       96,    # cold marine air, pitcher-friendly
        'dome':           False,
        'altitude_ft':    10,
    },
    'Seattle Mariners': {
        'park_name':      'T-Mobile Park',
        'general_factor': 96,
        'k_factor':       104,   # large foul territory catches more Ks
        'dome':           False,
        'altitude_ft':    175,
    },
    'St. Louis Cardinals': {
        'park_name':      'Busch Stadium',
        'general_factor': 98,
        'k_factor':       98,
        'dome':           False,
        'altitude_ft':    465,
    },
    'Tampa Bay Rays': {
        'park_name':      'Tropicana Field',
        'general_factor': 95,
        'k_factor':       109,   # dome + artificial turf creates unusual K conditions
        'dome':           True,
        'altitude_ft':    15,
    },
    'Texas Rangers': {
        'park_name':      'Globe Life Field',
        'general_factor': 104,
        'k_factor':       102,
        'dome':           True,
        'altitude_ft':    551,
    },
    'Toronto Blue Jays': {
        'park_name':      'Rogers Centre',
        'general_factor': 100,
        'k_factor':       101,
        'dome':           True,
        'altitude_ft':    76,
    },
    'Washington Nationals': {
        'park_name':      'Nationals Park',
        'general_factor': 100,
        'k_factor':       100,
        'dome':           False,
        'altitude_ft':    25,
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Part 2 — Park adjustment function
# ──────────────────────────────────────────────────────────────────────────────

def get_park_k_adjustment(home_team: str) -> tuple[float, str, int]:
    """
    Return (k_multiplier, park_label, k_factor) for the given home team name.

    Coors Field is handled as a special case due to extreme altitude effects.
    All other parks are bucketed by k_factor value.

    Falls back to (1.0, 'Neutral park', 100) for unknown teams.
    """
    entry = PARK_FACTORS.get(home_team)

    if entry is None:
        # Try partial match on team nickname
        lower = home_team.lower().strip()
        for key, val in PARK_FACTORS.items():
            if lower in key.lower() or key.lower() in lower:
                entry = val
                break

    if entry is None:
        return 1.0, 'Neutral park', 100

    k = entry['k_factor']
    alt = entry.get('altitude_ft', 0)

    # Coors Field override — altitude effect dominates
    if alt > 4000:
        return 0.94, 'Coors — significant K suppressor', k

    if k > 108:
        return 1.04, 'K-boosting park', k
    elif k >= 103:
        return 1.02, 'Slight K boost', k
    elif k >= 97:
        return 1.00, 'Neutral park', k
    elif k >= 92:
        return 0.98, 'Slight K suppressor', k
    else:
        return 0.96, 'K-suppressing park', k


def home_team_from_matchup(matchup: str) -> str:
    """
    Extract the home team name from an 'Away @ Home' matchup string.
    Returns empty string if format is unrecognised.
    """
    parts = str(matchup).strip().split(' @ ')
    if len(parts) == 2:
        return parts[1].strip()
    return ''


if __name__ == '__main__':
    print(f'{"Team":<28} {"Park":<32} {"Gen":>4} {"K":>4} {"Mult":>5} {"Label"}')
    print('-' * 90)
    for team, data in PARK_FACTORS.items():
        mult, label, k = get_park_k_adjustment(team)
        print(
            f'{team:<28} {data["park_name"]:<32} '
            f'{data["general_factor"]:>4} {k:>4} {mult:>5.2f}  {label}'
        )

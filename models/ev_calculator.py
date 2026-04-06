"""
models/ev_calculator.py — Core brain of Playbook.

Three layers:
  1. Pure math  — EV, implied probability, Kelly Criterion
  2. Model      — Poisson (strikeouts) + Normal (innings) probability models
  3. Pipeline   — reads today's files, matches props to stats, flags edges

Prop types supported:
  pitcher_strikeouts  — Over/Under on total Ks in today's start
  pitcher_innings     — Over/Under on innings pitched (e.g. over 5.5)

Usage:
    python models/ev_calculator.py
"""

import os
import sys
import math
import numpy as np
import pandas as pd
from scipy.stats import poisson, norm

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

RAW       = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw')
PROCESSED = os.path.join(os.path.dirname(__file__), '..', 'data', 'processed')
HIST      = os.path.join(os.path.dirname(__file__), '..', 'data', 'historical')

EV_THRESHOLD = 0.02   # flag anything above 2% EV
MIN_EV_KELLY = 0.01   # don't recommend a stake below 1% EV

# ── Tier-based Kelly caps ────────────────────────────────────────────────────
# Each tier has a different maximum fraction of bankroll we're willing to risk.
# Degen bets ignore Kelly math entirely and always get a flat $7 token stake.
KELLY_CAPS = {
    'CONSERVATIVE': 0.03,   # up to 3% of bankroll
    'MODERATE':     0.02,   # up to 2% of bankroll
    'AGGRESSIVE':   0.005,  # up to 0.5% of bankroll
}
DEGEN_FLAT_STAKE = 7.0      # flat dollar stake for any Degen (20%+ EV) signal


def ev_tier(ev: float) -> str:
    """Classify an EV value into its tier label."""
    if ev >= 0.20: return 'DEGEN'
    if ev >= 0.12: return 'AGGRESSIVE'
    if ev >= 0.07: return 'MODERATE'
    return 'CONSERVATIVE'

# ── Batter matchup: team name → team_krates.csv code ──────────
# team_krates uses Statcast abbreviations; pitcher_stats uses FanGraphs.
# This mapping handles full names, short names, city names, and both
# abbreviation formats so we can always find the right row.
TEAM_CODES = {
    # Direct passthrough (already in team_krates format)
    'ath': 'ATH', 'atl': 'ATL', 'az': 'AZ',  'bal': 'BAL', 'bos': 'BOS',
    'chc': 'CHC', 'cin': 'CIN', 'cle': 'CLE', 'col': 'COL', 'cws': 'CWS',
    'det': 'DET', 'hou': 'HOU', 'kc':  'KC',  'laa': 'LAA', 'lad': 'LAD',
    'mia': 'MIA', 'mil': 'MIL', 'min': 'MIN', 'nym': 'NYM', 'nyy': 'NYY',
    'phi': 'PHI', 'pit': 'PIT', 'sd':  'SD',  'sea': 'SEA', 'sf':  'SF',
    'stl': 'STL', 'tb':  'TB',  'tex': 'TEX', 'tor': 'TOR', 'wsh': 'WSH',
    # FanGraphs abbreviations that differ from Statcast
    'ari': 'AZ', 'oak': 'ATH', 'was': 'WSH', 'tam': 'TB',
    # Team nicknames (from matchup strings like "Cardinals @ Tigers")
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
    # City names
    'arizona': 'AZ',   'atlanta': 'ATL',   'baltimore': 'BAL',
    'boston': 'BOS',   'chicago': None,    'cincinnati': 'CIN',
    'cleveland': 'CLE','colorado': 'COL',  'detroit': 'DET',
    'houston': 'HOU',  'kansas city': 'KC','los angeles': None,
    'miami': 'MIA',    'milwaukee': 'MIL', 'minnesota': 'MIN',
    'new york': None,  'oakland': 'ATH',   'philadelphia': 'PHI',
    'pittsburgh': 'PIT','san diego': 'SD', 'san francisco': 'SF',
    'seattle': 'SEA',  'st. louis': 'STL', 'tampa bay': 'TB',
    'texas': 'TEX',    'toronto': 'TOR',   'washington': 'WSH',
}


# ============================================================
# LAYER 1 — Pure math functions
# ============================================================

def american_to_decimal(american_odds: float) -> float:
    """
    Convert American odds to decimal odds.
    +150  ->  2.50   (win $1.50 per $1 bet, get back $2.50)
    -150  ->  1.667  (win $0.667 per $1 bet, get back $1.667)
    """
    if american_odds >= 0:
        return (american_odds / 100) + 1
    else:
        return (100 / abs(american_odds)) + 1


def american_to_implied_prob(american_odds: float) -> float:
    """
    Convert American odds to the book's implied probability.
    Includes the vig (house edge), so this is slightly inflated.
    +150  ->  0.400  (40% chance implied)
    -150  ->  0.600  (60% chance implied)
    """
    if american_odds >= 0:
        return 100 / (american_odds + 100)
    else:
        return abs(american_odds) / (abs(american_odds) + 100)


def remove_vig(over_odds: float, under_odds: float) -> tuple[float, float]:
    """
    Remove the book's vig to get fair (no-vig) probabilities.
    Books inflate both sides so they profit regardless of outcome.
    Returns (fair_over_prob, fair_under_prob).
    """
    raw_over  = american_to_implied_prob(over_odds)
    raw_under = american_to_implied_prob(under_odds)
    total     = raw_over + raw_under          # always > 1.0 due to vig
    return raw_over / total, raw_under / total


def calculate_ev(model_prob: float, american_odds: float) -> float:
    """
    Expected Value per $1 bet.

    EV = (model_prob x payout) - (1 - model_prob)

    Where payout = what you WIN per $1 (decimal odds - 1).

    Positive EV means the bet is profitable long-term.
    Example: EV of 0.06 means you expect to earn $0.06 per $1 bet.
    """
    decimal = american_to_decimal(american_odds)
    payout  = decimal - 1        # profit per $1 wagered
    ev      = (model_prob * payout) - (1 - model_prob)
    return round(ev, 4)


def kelly_stake(model_prob: float, american_odds: float,
                bankroll: float = 1000.0, max_kelly: float = 0.03) -> dict:
    """
    Kelly Criterion — how much of your bankroll to bet.

    Kelly % = (b*p - q) / b
    Where:
      b = decimal payout (odds - 1)
      p = model probability of winning
      q = 1 - p

    We use half-Kelly, then apply the tier-specific max_kelly cap.
    Returns a dict with fraction, dollar amount, cap_applied flag, and note.
    """
    decimal = american_to_decimal(american_odds)
    b = decimal - 1
    p = model_prob
    q = 1 - p

    if b <= 0:
        return {'fraction': 0, 'dollars': 0, 'cap_applied': False, 'note': 'Invalid odds'}

    full_kelly = (b * p - q) / b
    half_kelly = full_kelly / 2

    if half_kelly <= 0:
        return {'fraction': 0, 'dollars': 0, 'cap_applied': False, 'note': 'Negative edge — no bet'}

    cap_applied = half_kelly > max_kelly
    capped      = min(half_kelly, max_kelly)
    dollars     = round(bankroll * capped, 2)

    return {
        'fraction':    round(capped, 4),
        'dollars':     dollars,
        'cap_applied': cap_applied,
        'note':        f'Half-Kelly capped at {max_kelly:.1%}' if cap_applied else 'Half-Kelly',
    }


# ============================================================
# LAYER 2 — Pitcher K probability model (Poisson)
# ============================================================

def estimate_expected_ks(k9: float, ip_per_start: float) -> float:
    """
    Expected strikeouts in today's start.

    K/9 tells us Ks per 9 innings. We scale by expected innings today.
    Expected Ks = K/9 * (IP per start / 9)
    """
    return k9 * (ip_per_start / 9)


def prob_over_line(expected_ks: float, line: float) -> float:
    """
    Probability the pitcher goes OVER the strikeout line.

    Uses a Poisson distribution — the standard model for count events.
    Lines are typically X.5 (e.g. 5.5), so:
      P(over 5.5) = P(K >= 6) = 1 - P(K <= 5)
    """
    k_floor = math.floor(line)   # for a line of 5.5, floor = 5
    return round(1 - poisson.cdf(k_floor, expected_ks), 4)


def prob_under_line(expected_ks: float, line: float) -> float:
    """
    Probability the pitcher goes UNDER the strikeout line.
    """
    return round(1 - prob_over_line(expected_ks, line), 4)


# ── Innings model (Normal distribution) ─────────────────────
# Innings pitched per start behaves like a Normal distribution:
# most starts cluster around the pitcher's average with a
# standard deviation of ~1.2 innings.

IP_STD_DEFAULT = 1.2   # typical start-to-start variation in innings

def prob_over_innings(avg_ip: float, line: float,
                      ip_std: float = IP_STD_DEFAULT) -> float:
    """
    Probability the pitcher records MORE than `line` innings.

    Uses a Normal distribution centred on the pitcher's historical
    average IP per start. Example: avg_ip=6.1, line=5.5 → likely over.
    """
    return round(1 - norm.cdf(line, loc=avg_ip, scale=ip_std), 4)


def prob_under_innings(avg_ip: float, line: float,
                       ip_std: float = IP_STD_DEFAULT) -> float:
    return round(1 - prob_over_innings(avg_ip, line, ip_std), 4)


# ============================================================
# LAYER 3 — Pipeline: match props → stats → EV signals
# ============================================================

def normalize_name(name: str) -> str:
    """Lowercase, strip whitespace, drop suffixes like Jr./Sr./III."""
    if not isinstance(name, str):
        return ''
    name = name.lower().strip()
    for suffix in [' jr.', ' sr.', ' jr', ' sr', ' iii', ' ii', ' iv']:
        name = name.replace(suffix, '')
    return name.strip()


def match_name(target: str, candidates: pd.Series) -> str | None:
    """
    Try to find target name in a Series of candidate names.
    1. Exact normalized match
    2. Last name match
    Returns the matched candidate or None.
    """
    norm_target    = normalize_name(target)
    norm_candidates = candidates.apply(normalize_name)

    # Exact
    exact = norm_candidates[norm_candidates == norm_target]
    if not exact.empty:
        return candidates[exact.index[0]]

    # Last name fallback
    last = norm_target.split()[-1] if norm_target else ''
    if last:
        last_match = norm_candidates[norm_candidates.str.endswith(last)]
        if len(last_match) == 1:
            return candidates[last_match.index[0]]

    return None


def team_name_to_code(name: str) -> str | None:
    """
    Convert any team name/abbreviation to the 3-letter code used in team_krates.csv.
    Tries the full string first, then each word individually as a fallback.
    Returns None if no match found.
    """
    key = name.lower().strip()
    if key in TEAM_CODES:
        return TEAM_CODES[key]
    # Try each word (catches "Tigers" from "Detroit Tigers")
    for word in key.split():
        if word in TEAM_CODES and TEAM_CODES[word] is not None:
            return TEAM_CODES[word]
    return None


def lookup_opp_krate(pitcher_team: str, matchup: str,
                     throws: str, krates_df: pd.DataFrame) -> tuple:
    """
    Given a pitcher's team, the matchup string, and the pitcher's throwing hand,
    return (opp_team_code, opp_k_pct, matchup_factor).

    matchup_factor = opp_k_pct / league_avg_k_pct
      > 1.0  →  strikeout-prone lineup  →  bump expected Ks up
      < 1.0  →  hard-to-strikeout lineup →  trim expected Ks down
      = 1.0  →  no data available, no adjustment

    Example: opp K% = 0.270, league avg = 0.225 → factor = 1.20
    """
    if krates_df is None or not matchup:
        return None, None, 1.0

    # Normalise pitcher's team to krates format
    pitcher_code = team_name_to_code(pitcher_team) or pitcher_team.upper()

    # Parse "Cardinals @ Tigers" → away='Cardinals', home='Tigers'
    parts = str(matchup).replace(' @ ', '|').split('|')
    if len(parts) != 2:
        return None, None, 1.0

    away_code = team_name_to_code(parts[0].strip())
    home_code = team_name_to_code(parts[1].strip())

    # Opposing team = whichever side doesn't match the pitcher's team
    if away_code and away_code != pitcher_code:
        opp_code = away_code
    elif home_code and home_code != pitcher_code:
        opp_code = home_code
    else:
        return None, None, 1.0

    col = 'vs_RHP' if str(throws).upper() == 'R' else 'vs_LHP'
    row = krates_df[krates_df['team'] == opp_code]
    if row.empty or pd.isna(row.iloc[0].get(col)):
        return opp_code, None, 1.0

    opp_k_pct   = float(row.iloc[0][col])
    league_avg  = float(krates_df[col].mean())
    factor      = round(opp_k_pct / league_avg, 3) if league_avg > 0 else 1.0

    return opp_code, round(opp_k_pct, 3), factor


def _weather_cols(matchup: str, weather_lookup: dict) -> dict:
    """
    Look up weather for the home team of this matchup.
    Handles both "Away @ Home" format (live props) and bare team
    name/code (synthetic props).
    Returns a dict of four weather columns (all None/0 if no data).
    """
    m = str(matchup).strip()
    home_str  = m.split(' @ ')[-1].strip() if ' @ ' in m else m
    home_code = team_name_to_code(home_str) if home_str else None
    wx        = weather_lookup.get(home_code) if home_code else None

    if wx is None:
        return {
            'weather_wind_label':  None,
            'weather_wind_factor': 0.0,
            'weather_temp_f':      None,
            'weather_precip_pct':  None,
        }

    temp  = wx.get('temperature_f')
    prec  = wx.get('precip_pct')
    return {
        'weather_wind_label':  str(wx.get('wind_label', '')),
        'weather_wind_factor': float(wx.get('wind_factor', 0.0)),
        'weather_temp_f':      float(temp) if temp is not None and pd.notna(temp) else None,
        'weather_precip_pct':  int(float(prec)) if prec is not None and pd.notna(prec) else None,
    }


def build_ev_signals(props_df:    pd.DataFrame,
                     savant_df:   pd.DataFrame,
                     stats_df:    pd.DataFrame,
                     bankroll:    float = 1000.0,
                     baselines_df: pd.DataFrame | None = None,
                     krates_df:   pd.DataFrame | None = None,
                     umpire_data: dict | None = None,
                     all_stats_df: pd.DataFrame | None = None,
                     weather_df:  pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Core matching and EV calculation.

    For each prop line:
      1. Look up the pitcher's stats
      2. Blend current K/9 with historical baseline (60/40 weighting)
      3. Estimate expected Ks via Poisson
      4. Calculate model probability for Over and Under
      5. Calculate EV for both sides
      6. Flag rows above EV_THRESHOLD

    umpire_data: dict returned by umpire_scraper.get_todays_umpires().
                 Keys are normalised '{away} @ {home}' matchup strings.
                 Applied after the batter matchup adjustment.
    """
    from models.player_baseline import lookup_baseline

    rows = []

    # Build weather lookup: team_code → row from weather_today.csv
    weather_lookup: dict = {}
    if weather_df is not None and not weather_df.empty:
        for _, wr in weather_df.iterrows():
            code = str(wr.get('home_team', '')).strip().upper()
            if code:
                weather_lookup[code] = wr

    # Build total historical GS lookup: {normalized_name: total_GS_2024+2025}.
    # Pitchers with fewer than 8 combined starts across both seasons are flagged
    # as low_history — injury returnees or prospects with too small a track record
    # to trust even the historical baseline.
    all_stats_gs: dict = {}
    if all_stats_df is not None:
        gs_col   = 'GS' if 'GS' in all_stats_df.columns else (
                   'starts' if 'starts' in all_stats_df.columns else None)
        name_col = 'Name' if 'Name' in all_stats_df.columns else 'name'
        if gs_col and name_col in all_stats_df.columns:
            for _, arow in all_stats_df.iterrows():
                nkey = normalize_name(str(arow.get(name_col, '')))
                if nkey:
                    all_stats_gs[nkey] = all_stats_gs.get(nkey, 0) + int(arow.get(gs_col, 0) or 0)

    for _, prop in props_df.iterrows():
        player = prop['player']

        # ── Low history detection ─────────────────────────────────────────────
        # Check combined career GS across 2024+2025 from pitcher_stats_all.csv.
        # Fewer than 8 starts = not enough history to trust the baseline,
        # regardless of current season start count.
        norm_player   = normalize_name(player)
        total_hist_gs = all_stats_gs.get(norm_player, 0)
        low_history   = total_hist_gs < 8

        # --- Match to FanGraphs stats (K/9, IP per start) ---
        stats_name = match_name(player, stats_df['name'])
        if stats_name:
            st = stats_df[stats_df['name'] == stats_name].iloc[0]
            k9           = float(st['k9'])
            curr_starts  = float(st.get('starts', 0))
            ip_per_start = float(st['ip']) / max(curr_starts, 1)
        else:
            curr_starts = 0.0
            # Fall back to Statcast K% * ~24 estimated batters faced
            sv_name = match_name(player, savant_df['name'])
            if sv_name:
                sv = savant_df[savant_df['name'] == sv_name].iloc[0]
                if pd.notna(sv.get('k_pct')):
                    # Convert K% to approximate K/9
                    # ~4.3 batters per inning, so K/9 ≈ k_pct * 4.3 * 9
                    k9 = float(sv['k_pct']) * 38.7
                    ip_per_start = 5.5   # league average default
                else:
                    continue   # no stats available — skip
            else:
                continue   # pitcher not found in any dataset — skip

        line       = float(prop['line'])
        over_odds  = float(prop['over_odds'])
        under_odds = float(prop.get('under_odds', over_odds))

        # ── Historical baseline blending ─────────────────────
        # If we have 2yr history, blend: 60% current K/9 + 40% historical K/9
        # This prevents fluky small-sample current stats from dominating.
        # Also carry trend/reliability into the signal rows.
        hist_k9          = None
        hist_reliability = None
        k9_trend         = 'NEW'
        blended_k9       = k9   # default: unblended

        if baselines_df is not None:
            bl = lookup_baseline(player, baselines_df)
            if bl and pd.notna(bl.get('hist_k9')):
                hist_k9          = float(bl['hist_k9'])
                hist_reliability = int(bl.get('reliability', 50))
                k9_trend         = bl.get('k9_trend', 'NEW')

                # Base weight from reliability (30–50% historical)
                base_hist = 0.30 + (hist_reliability / 100) * 0.20

                # Early-season blending — hard floors on historical weight
                # to prevent tiny samples from distorting the model.
                # <4 starts : 92% historical, 8% current
                # 4-6 starts: 80% historical, 20% current
                # 7-9 starts: ramp from base_hist + early_bonus toward normal
                # 10+ starts: normal reliability-based weight (no floor)
                if low_history:
                    # Injury returnee / low career track record.
                    # Force 95% historical regardless of current start count —
                    # we barely know this version of the pitcher yet.
                    hist_weight = 0.95
                elif curr_starts < 4:
                    hist_weight = 0.92
                elif curr_starts <= 6:
                    hist_weight = 0.80
                else:
                    early_bonus = max(0.0, (1.0 - curr_starts / 10.0)) * 0.40
                    hist_weight = min(base_hist + early_bonus, 0.85)

                curr_weight = 1 - hist_weight
                blended_k9  = curr_weight * k9 + hist_weight * hist_k9

        prop_type = str(prop.get('prop_type', 'pitcher_strikeouts'))

        # ── Savant context: velocity trend, spin rate, pitch mix, handedness ──
        sv_lookup = match_name(player, savant_df['name'])
        sv_row    = savant_df[savant_df['name'] == sv_lookup].iloc[0] if sv_lookup else None

        throws     = 'R'
        velo_trend = None
        spin_rate  = None
        pitch_mix  = None

        if sv_row is not None:
            t = sv_row.get('throws')
            if t and pd.notna(t):
                throws = str(t)
            vt = sv_row.get('velo_trend')
            if vt is not None and pd.notna(vt):
                velo_trend = float(vt)
            sr = sv_row.get('spin_rate')
            if sr is not None and pd.notna(sr):
                spin_rate = float(sr)
            pm = sv_row.get('pitch_mix')
            if pm is not None and pd.notna(pm):
                pitch_mix = str(pm)
            # Blend current avg_ip with historical, using the same early-season
            # schedule as K/9.  This prevents a pitcher with 2 short starts
            # from collapsing expected innings far below their true baseline.
            #
            # <4 starts : 92% hist, 8% current
            # 4–6 starts: 80% hist, 20% current
            # 7–9 starts: ramp from 80% → 30% historical
            # 10+ starts : trust current data fully
            ai      = sv_row.get('avg_ip')
            hist_ai = sv_row.get('hist_avg_ip')
            if ai is not None and pd.notna(ai) and float(ai) > 0:
                if hist_ai is not None and pd.notna(hist_ai) and float(hist_ai) > 0:
                    if curr_starts < 4:
                        hist_w = 0.92
                    elif curr_starts <= 6:
                        hist_w = 0.80
                    elif curr_starts <= 9:
                        ramp   = (curr_starts - 6) / 3   # 0→1 over starts 7–9
                        hist_w = 0.80 - ramp * 0.50       # 0.80 → 0.30
                    else:
                        hist_w = 0.0
                    ip_per_start = round((1 - hist_w) * float(ai) + hist_w * float(hist_ai), 2)
                else:
                    ip_per_start = float(ai)

        # ── Velocity trend adjustment ─────────────────────────────────────────
        # Compares last 7 days of fastball velo vs the full 30-day avg.
        # +1 mph over last week → ~1.5% boost to expected Ks (pitcher gaining steam).
        # -1 mph → ~1.5% reduction (losing velo is a warning sign).
        # Capped at ±6% so a single hot/cold week can't swing the model too far.
        velo_factor = 1.0
        if velo_trend is not None and prop_type != 'pitcher_innings':
            velo_factor = 1.0 + max(min(velo_trend * 0.015, 0.06), -0.06)

        # ── Batter matchup context ────────────────────────────
        # Look up the opposing lineup's K-rate vs this pitcher's hand.
        # A strikeout-prone lineup boosts expected Ks; a contact lineup lowers them.
        # Only applies to strikeout props (innings props are unaffected by K-rate).
        opp_team       = None
        opp_k_pct      = None
        matchup_factor = 1.0

        if krates_df is not None and prop_type != 'pitcher_innings':
            # Pitcher's team code from stats_df
            pitcher_team = ''
            if stats_name:
                pitcher_team = str(stats_df[stats_df['name'] == stats_name].iloc[0].get('team', ''))

            opp_team, opp_k_pct, matchup_factor = lookup_opp_krate(
                pitcher_team, str(prop.get('matchup', '')), throws, krates_df
            )

        # ── Umpire adjustment ─────────────────────────────────────────────
        # Match this prop's matchup to today's home plate umpire profile.
        # Tight zone (>60th pct) → +3% K probability boost.
        # Large zone (<40th pct) → -3% K probability reduction.
        # Inverse applied to innings props (more Ks → longer games → more IP).
        # No data available → factor stays 1.0, note logged.
        umpire_name       = None
        umpire_adjustment = 1.0
        umpire_note       = 'no umpire data'

        if umpire_data:
            matchup_str = str(prop.get('matchup', '')).lower().strip()
            # Direct match first; then try any entry whose key overlaps
            ump_info = umpire_data.get(matchup_str)
            if ump_info is None:
                # Partial match: check if any umpire matchup key is contained
                # in or contains the prop's matchup string
                for k, v in umpire_data.items():
                    parts = k.split(' @ ')
                    if len(parts) == 2:
                        if parts[0] in matchup_str or parts[1] in matchup_str:
                            ump_info = v
                            break

            if ump_info:
                umpire_name       = ump_info.get('umpire_name')
                umpire_adjustment = float(ump_info.get('k_factor', 1.0))
                umpire_note       = ump_info.get('note', '')

        # Apply matchup, velocity trend, and umpire factors to blended K/9
        adjusted_k9 = blended_k9 * matchup_factor * velo_factor * umpire_adjustment

        # ── Route to the correct probability model ────────────
        if prop_type == 'pitcher_innings':
            # Normal distribution around historical avg IP/start
            model_over  = prob_over_innings(ip_per_start, line)
            model_under = prob_under_innings(ip_per_start, line)
            expected_ks = None   # not applicable for innings props
        else:
            # Default: pitcher_strikeouts — Poisson model
            expected_ks = estimate_expected_ks(adjusted_k9, ip_per_start)
            model_over  = prob_over_line(expected_ks, line)
            model_under = prob_under_line(expected_ks, line)

        # ── Fix 3: Low line confidence reduction ─────────────────────────────
        # Very low strikeout lines (≤3.5) are highly sensitive to small
        # changes in game script (early hook, rain delay, quick inning).
        # We discount the model probability to avoid over-betting these.
        # ≤2.5 line → multiply by 0.80  |  ≤3.5 line → multiply by 0.88
        low_line_note = None
        if prop_type == 'pitcher_strikeouts':
            if line <= 2.5:
                model_over  = round(model_over  * 0.80, 4)
                model_under = round(model_under * 0.80, 4)
                low_line_note = 'Low line discount 0.80x (line <= 2.5)'
            elif line <= 3.5:
                model_over  = round(model_over  * 0.88, 4)
                model_under = round(model_under * 0.88, 4)
                low_line_note = 'Low line discount 0.88x (line <= 3.5)'

        # ── Model probability ceiling ─────────────────────────────────────────
        # Cap probability before it enters EV.
        # Normal ceiling: 0.70 (down from 0.75 — real-world variance warrants caution).
        # Low-history ceiling: 0.65 — even less confidence on returnees/prospects.
        PROB_CEILING = 0.65 if low_history else 0.70
        over_capped  = model_over  > PROB_CEILING
        under_capped = model_under > PROB_CEILING
        model_over   = min(model_over,  PROB_CEILING)
        model_under  = min(model_under, PROB_CEILING)

        book_over_prob, book_under_prob = remove_vig(over_odds, under_odds)

        ev_over  = calculate_ev(model_over,  over_odds)
        ev_under = calculate_ev(model_under, under_odds)

        # ── Tier-based Kelly sizing ───────────────────────────────────────
        # Classify each side by its EV, then apply the matching cap.
        # Degen (20%+ EV) skips the Kelly formula entirely — flat $7 stake.
        tier_over  = ev_tier(ev_over)
        tier_under = ev_tier(ev_under)

        if tier_over == 'DEGEN':
            kelly_over = {
                'fraction': 0.0, 'dollars': DEGEN_FLAT_STAKE,
                'cap_applied': False, 'note': 'Degen flat stake',
            }
        else:
            kelly_over = kelly_stake(model_over, over_odds, bankroll,
                                     max_kelly=KELLY_CAPS[tier_over])

        if tier_under == 'DEGEN':
            kelly_under = {
                'fraction': 0.0, 'dollars': DEGEN_FLAT_STAKE,
                'cap_applied': False, 'note': 'Degen flat stake',
            }
        else:
            kelly_under = kelly_stake(model_under, under_odds, bankroll,
                                      max_kelly=KELLY_CAPS[tier_under])

        # Pull xFIP for the Discord embed (nice-to-have, not required)
        xfip_val = None
        if stats_name:
            st = stats_df[stats_df['name'] == stats_name].iloc[0]
            xfip_val = st.get('xfip') if pd.notna(st.get('xfip')) else None

        base = {
            'player':           player,
            'prop_type':        prop_type,
            'matchup':          prop.get('matchup', ''),
            'book':             prop.get('book', ''),
            'line':             line,
            'expected_ks':      round(expected_ks, 2) if expected_ks is not None else None,
            'k9_used':          round(adjusted_k9, 2),
            'k9_current':       round(k9, 2),
            'k9_historical':    round(hist_k9, 2) if hist_k9 is not None else None,
            'k9_trend':         k9_trend,
            'hist_reliability': hist_reliability,
            'ip_per_start':     round(ip_per_start, 1),
            'xfip':             xfip_val,
            'velo_trend':       round(velo_trend, 1) if velo_trend is not None else None,
            'velo_factor':      round(velo_factor, 3),
            'spin_rate':        round(spin_rate, 0) if spin_rate is not None else None,
            'pitch_mix':        pitch_mix,
            'throws':           throws,
            'opp_team':         opp_team,
            'opp_k_pct':        opp_k_pct,
            'matchup_factor':    matchup_factor,
            'low_line_note':     low_line_note,
            'umpire_name':       umpire_name,
            'umpire_adjustment': round(umpire_adjustment, 3),
            'low_history':       low_history,
            'ev_suspect':        False,   # overwritten in run() for live props with EV > 15%
            # ── Weather context (informational — not applied to Poisson model) ──
            **_weather_cols(prop.get('matchup', ''), weather_lookup),
        }

        # Over row
        rows.append({**base,
            'side':              'Over',
            'odds':              over_odds,
            'model_prob':        model_over,
            'implied_prob':      round(book_over_prob, 4),
            'ev':                ev_over,
            'kelly_pct':         kelly_over['fraction'],
            'kelly_dollars':     kelly_over['dollars'],
            'kelly_cap_applied': kelly_over['cap_applied'],
            'flag':              ev_over >= EV_THRESHOLD,
            'prob_capped':       over_capped,
        })

        # Under row
        rows.append({**base,
            'side':              'Under',
            'odds':              under_odds,
            'model_prob':        model_under,
            'implied_prob':      round(book_under_prob, 4),
            'ev':                ev_under,
            'kelly_pct':         kelly_under['fraction'],
            'kelly_dollars':     kelly_under['dollars'],
            'kelly_cap_applied': kelly_under['cap_applied'],
            'flag':              ev_under >= EV_THRESHOLD,
            'prob_capped':       under_capped,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values('ev', ascending=False).reset_index(drop=True)
    return df


def make_synthetic_props(stats_df: pd.DataFrame, savant_df: pd.DataFrame) -> pd.DataFrame:
    """
    When no real props file exists, generate realistic synthetic lines.

    STRIKEOUT lines: set ~10% BELOW expected Ks (as real books do)
    so the model sees genuine Over AND Under opportunities.

    INNINGS lines: set at 5.5 for most starters — the most common
    real-world line. Good starters averaging 6.5 IP show Over value;
    shaky starters averaging 5.0 show Under value.
    """
    rows = []
    for _, st in stats_df.iterrows():
        ip_per_start = float(st['ip']) / max(float(st['starts']), 1)
        exp_ks       = estimate_expected_ks(float(st['k9']), ip_per_start)

        rng = np.random.default_rng(seed=int(abs(hash(st['name'])) % 10000))
        sv_name = match_name(st['name'], savant_df['name'])
        matchup = savant_df[savant_df['name'] == sv_name].iloc[0]['team'] \
                  if sv_name else st['team']

        # ── Strikeout prop ──────────────────────────────────────
        # Real books shade lines ~10% below model expectation to
        # attract Over action. Setting at 85-90% of expected Ks
        # creates genuine two-sided markets.
        ks_line = max(0.5, round((exp_ks * rng.uniform(0.82, 0.92)) * 2) / 2)

        # Juice: Overs typically slight underdogs vs Unders early season
        over_odds_k  = int(rng.choice([-115, -110, -105, +100, +105, +110, +115]))
        under_map    = {-115: +105, -110: -105, -105: -110, +100: -115,
                        +105: -120, +110: -125, +115: -130}
        under_odds_k = under_map.get(over_odds_k, -110)

        rows.append({
            'player':     st['name'],
            'matchup':    matchup,
            'prop_type':  'pitcher_strikeouts',
            'line':       ks_line,
            'over_odds':  over_odds_k,
            'under_odds': under_odds_k,
            'book':       'Synthetic (no live props yet)',
        })

        # ── Innings pitched prop ────────────────────────────────
        # Standard market line is 5.5 IP for most starters.
        # Elite workhorses sometimes get 5.5/6.0 split markets.
        if ip_per_start >= 6.2:
            ip_line = 6.0
        elif ip_per_start >= 5.5:
            ip_line = 5.5
        else:
            ip_line = 4.5

        over_odds_ip  = int(rng.choice([-130, -120, -115, -110, -105, +100]))
        under_map_ip  = {-130: +110, -120: +100, -115: +105, -110: -110,
                         -105: -115, +100: -120}
        under_odds_ip = under_map_ip.get(over_odds_ip, -110)

        rows.append({
            'player':     st['name'],
            'matchup':    matchup,
            'prop_type':  'pitcher_innings',
            'line':       ip_line,
            'over_odds':  over_odds_ip,
            'under_odds': under_odds_ip,
            'book':       'Synthetic (no live props yet)',
        })

    return pd.DataFrame(rows)


# ============================================================
# Main
# ============================================================

def run():
    print('=' * 60)
    print('  PLAYBOOK -- EV Calculator')
    print('=' * 60)

    os.makedirs(PROCESSED, exist_ok=True)

    # Load pitcher stats
    stats_path  = os.path.join(RAW, 'pitcher_stats.csv')
    savant_path = os.path.join(RAW, 'savant_today.csv')
    props_path  = os.path.join(RAW, 'todays_props.csv')

    for path in [stats_path, savant_path]:
        if not os.path.exists(path):
            print(f'Missing required file: {path}')
            print('Run scrapers/baseball_savant.py and scrapers/fangraphs.py first.')
            return

    stats_df  = pd.read_csv(stats_path)
    savant_df = pd.read_csv(savant_path)

    # Load or synthesise props
    using_synthetic = False
    if os.path.exists(props_path):
        props_df = pd.read_csv(props_path)
        if props_df.empty:
            using_synthetic = True
    else:
        using_synthetic = True

    if using_synthetic:
        print('\nNo live props found — generating synthetic lines from K/9 data.')
        print('(Run scrapers/odds_api.py after ~9am ET on a game day for real props.)\n')
        props_df = make_synthetic_props(stats_df, savant_df)

    # Load historical baselines (optional — degrades gracefully if missing)
    baselines_path = os.path.join(HIST, 'player_baselines.csv')
    baselines_df   = pd.read_csv(baselines_path) if os.path.exists(baselines_path) else None
    if baselines_df is not None:
        print(f'Historical baselines loaded: {len(baselines_df)} pitchers (2024-2025)')
    else:
        print('No historical baselines found — run scrapers/historical_stats.py and models/player_baseline.py')

    # Load combined 2024+2025 stats for low_history detection (injury returnees / prospects)
    all_stats_path = os.path.join(HIST, 'pitcher_stats_all.csv')
    all_stats_df   = pd.read_csv(all_stats_path) if os.path.exists(all_stats_path) else None
    if all_stats_df is not None:
        print(f'All-season stats loaded: {len(all_stats_df)} rows (2024+2025) — low_history detection active')
    else:
        print('No all-season stats found — low_history detection disabled')

    print(f'Pitchers in FanGraphs stats: {len(stats_df)}')
    print(f'Pitchers in Statcast data:   {len(savant_df)}')
    print(f'Prop lines to evaluate:      {len(props_df)}')

    # Load team K-rates (optional — degrades gracefully if missing)
    krates_path = os.path.join(RAW, 'team_krates.csv')
    krates_df   = pd.read_csv(krates_path) if os.path.exists(krates_path) else None
    if krates_df is not None:
        print(f'Team K-rates loaded: {len(krates_df)} teams')
    else:
        print('No team K-rates found — run scrapers/fangraphs.py first')

    # Load weather (optional — degrades gracefully if missing)
    weather_path = os.path.join(RAW, 'weather_today.csv')
    weather_df   = pd.read_csv(weather_path) if os.path.exists(weather_path) else None
    if weather_df is not None:
        print(f'Weather loaded: {len(weather_df)} stadiums')
    else:
        print('No weather data — run scrapers/weather_scraper.py first')

    # Load umpire data (optional — degrades gracefully if unavailable)
    umpire_data = None
    try:
        from scrapers.umpire_scraper import get_todays_umpires, build_umpire_profiles
        profiles    = build_umpire_profiles()
        umpire_data = get_todays_umpires(profiles)
        if umpire_data:
            covered = sum(1 for v in umpire_data.values() if v.get('umpire_name'))
            print(f'Umpire data loaded: {covered}/{len(umpire_data)} games with profiles')
        else:
            print('Umpire data: no assignments found (off-day or API unavailable)')
    except Exception as e:
        print(f'Umpire data unavailable ({e}) — continuing without adjustment')

    bankroll = 1000.0
    signals  = build_ev_signals(props_df, savant_df, stats_df, bankroll,
                                baselines_df, krates_df, umpire_data, all_stats_df,
                                weather_df)

    if signals.empty:
        print('\nNo signals generated — check that pitcher names match across files.')
        return

    # ── EV sanity cap — suspect flagging ─────────────────────────────────────
    # Any live prop showing EV > 15% is almost certainly a data anomaly or
    # model error. Flag it ev_suspect = True. It still gets logged to CSV and
    # Supabase so we have a paper trail, but it is excluded from Discord alerts
    # and paper trading so we never act on it.
    if not using_synthetic:
        signals.loc[signals['ev'] > 0.18, 'ev_suspect'] = True
    suspect_count = int(signals['ev_suspect'].sum()) if not signals.empty else 0
    if suspect_count > 0:
        print(f'\n  ⚠  {suspect_count} signal(s) flagged ev_suspect (EV > 15% on live prop) — logged but excluded from Discord')

    # Save full results (includes suspect signals — for the paper trail)
    out_path = os.path.join(PROCESSED, 'ev_signals.csv')
    signals.to_csv(out_path, index=False)
    print(f'\nFull results saved to: {os.path.normpath(out_path)}')
    print(f'Total rows evaluated: {len(signals)}')

    # --- Flagged bets (EV >= 4%) ---
    flagged = signals[signals['flag']].copy()
    source  = 'SYNTHETIC' if using_synthetic else 'LIVE'

    print(f'\n{"=" * 60}')
    print(f'  POSITIVE EV PROPS ({source} DATA) — threshold: {EV_THRESHOLD:.0%}')
    print(f'{"=" * 60}')

    if flagged.empty:
        print(f'  No props exceed {EV_THRESHOLD:.0%} EV today.')
        print(f'  Closest plays:')
        closest = signals.head(5)[['player','prop_type','side','line','odds','model_prob','implied_prob','ev']]
        print(closest.to_string(index=False))
    else:
        # Print by prop type so they're easy to scan
        for ptype in ['pitcher_strikeouts', 'pitcher_innings']:
            subset = flagged[flagged['prop_type'] == ptype]
            if subset.empty:
                continue
            label = 'STRIKEOUT PROPS' if ptype == 'pitcher_strikeouts' else 'INNINGS PROPS'
            print(f'\n  -- {label} ({len(subset)} flagged) --')
            for _, r in subset.iterrows():
                edge = r['model_prob'] - r['implied_prob']
                print(
                    f"  {r['player']:<24} {r['side']:5}  {r['line']:.1f}  "
                    f"odds: {int(r['odds']):+d}  "
                    f"model: {r['model_prob']:.1%}  "
                    f"implied: {r['implied_prob']:.1%}  "
                    f"edge: {edge:+.1%}  "
                    f"EV: {r['ev']:+.1%}  "
                    f"Kelly: ${r['kelly_dollars']:.0f}"
                )

    # --- EV explanation ---
    print(f'\n--- How to read this ---')
    print(f'  model prob   = Playbook probability (Poisson for Ks / Normal for innings)')
    print(f'  implied prob = book probability after removing vig')
    print(f'  edge         = model prob minus implied prob (your advantage)')
    print(f'  EV           = expected profit per $1 bet')
    print(f'  Kelly $      = recommended stake from ${bankroll:.0f} bankroll')

    if using_synthetic:
        print(f'\n  NOTE: Synthetic — lines set below expected value to simulate real markets.')
        print(f'  Real props will show tighter edges (1-6% on good plays, not 10-40%).')
        print(f'  Degen-tier alerts on synthetic data are expected and not meaningful.')

    # Alerts and paper trades exclude ev_suspect signals.
    # Suspects are still in ev_signals.csv and Supabase for the paper trail.
    alerts_df = signals[~signals['ev_suspect']].copy() if 'ev_suspect' in signals.columns else signals

    # --- Fire Discord daily card ---
    print(f'\n--- Firing Discord Daily Card ---')
    try:
        from alerts.discord_alerts import send_daily_card
        send_daily_card(alerts_df, ev_threshold=EV_THRESHOLD, max_bets=5)
    except Exception as e:
        print(f'  Alert error: {e}')

    # --- Log paper trades ---
    print(f'\n--- Logging Paper Trades ---')
    try:
        from alerts.paper_trading import log_bets_from_signals
        log_bets_from_signals(alerts_df, ev_threshold=EV_THRESHOLD, max_bets=5)
    except Exception as e:
        print(f'  Paper trading error: {e}')

    # --- Log to Supabase ---
    print(f'\n--- Logging to Supabase ---')
    try:
        from database import log_ev_signals
        log_ev_signals(signals)
    except Exception as e:
        print(f'  Supabase error: {e}')


if __name__ == '__main__':
    run()

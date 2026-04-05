"""
models/ev_calculator.py — Core brain of Playbook.

Three layers:
  1. Pure math  — EV, implied probability, Kelly Criterion
  2. Model      — estimates pitcher K probability using Poisson distribution
  3. Pipeline   — reads today's files, matches props to stats, flags edges

Usage:
    python models/ev_calculator.py
"""

import os
import sys
import math
import numpy as np
import pandas as pd
from scipy.stats import poisson

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

RAW       = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw')
PROCESSED = os.path.join(os.path.dirname(__file__), '..', 'data', 'processed')
HIST      = os.path.join(os.path.dirname(__file__), '..', 'data', 'historical')

EV_THRESHOLD = 0.04   # flag anything above 4% EV
MIN_EV_KELLY = 0.01   # don't recommend a stake below 1% EV
MAX_KELLY    = 0.05   # cap stake at 5% of bankroll (half-Kelly safety)


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


def kelly_stake(model_prob: float, american_odds: float, bankroll: float = 1000.0) -> dict:
    """
    Kelly Criterion — how much of your bankroll to bet.

    Kelly % = (b*p - q) / b
    Where:
      b = decimal payout (odds - 1)
      p = model probability of winning
      q = 1 - p

    We use half-Kelly (capped at MAX_KELLY) to reduce variance.
    Returns a dict with fraction, dollar amount, and interpretation.
    """
    decimal = american_to_decimal(american_odds)
    b = decimal - 1
    p = model_prob
    q = 1 - p

    if b <= 0:
        return {'fraction': 0, 'dollars': 0, 'note': 'Invalid odds'}

    full_kelly = (b * p - q) / b
    half_kelly = full_kelly / 2

    if half_kelly <= 0:
        return {'fraction': 0, 'dollars': 0, 'note': 'Negative edge — no bet'}

    capped = min(half_kelly, MAX_KELLY)
    dollars = round(bankroll * capped, 2)

    return {
        'fraction': round(capped, 4),
        'dollars':  dollars,
        'note':     f'Half-Kelly capped at {MAX_KELLY:.0%}' if half_kelly > MAX_KELLY else 'Half-Kelly'
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


def build_ev_signals(props_df:    pd.DataFrame,
                     savant_df:   pd.DataFrame,
                     stats_df:    pd.DataFrame,
                     bankroll:    float = 1000.0,
                     baselines_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Core matching and EV calculation.

    For each prop line:
      1. Look up the pitcher's stats
      2. Blend current K/9 with historical baseline (60/40 weighting)
      3. Estimate expected Ks via Poisson
      4. Calculate model probability for Over and Under
      5. Calculate EV for both sides
      6. Flag rows above EV_THRESHOLD
    """
    from models.player_baseline import lookup_baseline

    rows = []

    for _, prop in props_df.iterrows():
        player = prop['player']

        # --- Match to FanGraphs stats (K/9, IP per start) ---
        stats_name = match_name(player, stats_df['name'])
        if stats_name:
            st = stats_df[stats_df['name'] == stats_name].iloc[0]
            k9         = float(st['k9'])
            ip_per_start = float(st['ip']) / max(float(st['starts']), 1)
        else:
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

                # Weight blend by reliability: high reliability = trust history more
                hist_weight = 0.30 + (hist_reliability / 100) * 0.20   # 30-50%
                curr_weight = 1 - hist_weight
                blended_k9  = curr_weight * k9 + hist_weight * hist_k9

        expected_ks = estimate_expected_ks(blended_k9, ip_per_start)
        model_over  = prob_over_line(expected_ks, line)
        model_under = prob_under_line(expected_ks, line)

        book_over_prob, book_under_prob = remove_vig(over_odds, under_odds)

        ev_over  = calculate_ev(model_over,  over_odds)
        ev_under = calculate_ev(model_under, under_odds)

        kelly_over  = kelly_stake(model_over,  over_odds,  bankroll)
        kelly_under = kelly_stake(model_under, under_odds, bankroll)

        # Pull xFIP for the Discord embed (nice-to-have, not required)
        xfip_val = None
        if stats_name:
            st = stats_df[stats_df['name'] == stats_name].iloc[0]
            xfip_val = st.get('xfip') if pd.notna(st.get('xfip')) else None

        base = {
            'player':           player,
            'matchup':          prop.get('matchup', ''),
            'book':             prop.get('book', ''),
            'line':             line,
            'expected_ks':      round(expected_ks, 2),
            'k9_used':          round(blended_k9, 2),
            'k9_current':       round(k9, 2),
            'k9_historical':    round(hist_k9, 2) if hist_k9 is not None else None,
            'k9_trend':         k9_trend,
            'hist_reliability': hist_reliability,
            'ip_per_start':     round(ip_per_start, 1),
            'xfip':             xfip_val,
        }

        # Over row
        rows.append({**base,
            'side':          'Over',
            'odds':          over_odds,
            'model_prob':    model_over,
            'implied_prob':  round(book_over_prob, 4),
            'ev':            ev_over,
            'kelly_pct':     kelly_over['fraction'],
            'kelly_dollars': kelly_over['dollars'],
            'flag':          ev_over >= EV_THRESHOLD,
        })

        # Under row
        rows.append({**base,
            'side':          'Under',
            'odds':          under_odds,
            'model_prob':    model_under,
            'implied_prob':  round(book_under_prob, 4),
            'ev':            ev_under,
            'kelly_pct':     kelly_under['fraction'],
            'kelly_dollars': kelly_under['dollars'],
            'flag':          ev_under >= EV_THRESHOLD,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values('ev', ascending=False).reset_index(drop=True)
    return df


def make_synthetic_props(stats_df: pd.DataFrame, savant_df: pd.DataFrame) -> pd.DataFrame:
    """
    When no real props file exists, generate realistic synthetic lines
    from each pitcher's K/9 so we can demonstrate the model.

    Line = expected Ks rounded to nearest .5
    Odds = typical market pricing with slight variation.
    """
    rows = []
    for _, st in stats_df.iterrows():
        ip_per_start = float(st['ip']) / max(float(st['starts']), 1)
        exp_ks       = estimate_expected_ks(float(st['k9']), ip_per_start)

        # Set line just above/below expected to create over/under tension
        line = round(exp_ks * 2) / 2   # round to nearest .5

        # Vary the juice slightly per pitcher to simulate real market
        rng       = np.random.default_rng(seed=int(abs(hash(st['name'])) % 10000))
        over_odds = int(rng.choice([-125, -120, -115, -110, -105, +100, +105, +110]))
        under_odds_map = {-125: +105, -120: +100, -115: -105, -110: -110,
                          -105: -115, +100: -120, +105: -125, +110: -130}
        under_odds = under_odds_map.get(over_odds, -110)

        # Match to savant for team name
        sv_name = match_name(st['name'], savant_df['name'])
        matchup  = savant_df[savant_df['name'] == sv_name].iloc[0]['team'] if sv_name else st['team']

        rows.append({
            'player':     st['name'],
            'matchup':    matchup,
            'prop_type':  'pitcher_strikeouts',
            'line':       line,
            'over_odds':  over_odds,
            'under_odds': under_odds,
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

    print(f'Pitchers in FanGraphs stats: {len(stats_df)}')
    print(f'Pitchers in Statcast data:   {len(savant_df)}')
    print(f'Prop lines to evaluate:      {len(props_df)}')

    bankroll = 1000.0
    signals  = build_ev_signals(props_df, savant_df, stats_df, bankroll, baselines_df)

    if signals.empty:
        print('\nNo signals generated — check that pitcher names match across files.')
        return

    # Save full results
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
        closest = signals.head(5)[['player','side','line','odds','model_prob','implied_prob','ev']]
        print(closest.to_string(index=False))
    else:
        print(f'  {len(flagged)} props flagged\n')
        for _, r in flagged.iterrows():
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
    print(f'  model prob  = Playbook\'s estimated probability (Poisson + K/9)')
    print(f'  implied prob = book\'s probability after removing vig')
    print(f'  edge        = model prob minus implied prob (your advantage)')
    print(f'  EV          = expected profit per $1 bet')
    print(f'  Kelly $     = recommended stake from ${bankroll:.0f} bankroll')

    if using_synthetic:
        print(f'\n  NOTE: Synthetic props are generated from each pitcher\'s K/9.')
        print(f'  Lines are set at expected Ks; odds are randomised. Real props')
        print(f'  will show different lines and tighter edges.')

    # --- Fire Discord alerts ---
    print(f'\n--- Firing Discord Alerts ---')
    try:
        from alerts.discord_alerts import fire_alerts_from_signals
        fire_alerts_from_signals(signals, ev_threshold=EV_THRESHOLD, max_alerts=5)
    except Exception as e:
        print(f'  Alert error: {e}')


if __name__ == '__main__':
    run()

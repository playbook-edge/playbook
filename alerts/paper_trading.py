"""
alerts/paper_trading.py

Simulated paper trading tracker for Playbook.

Every time the EV calculator flags a bet, this module:
  1. Logs it to data/processed/paper_trades.csv
  2. Sends a "BET PLACED" embed to the paper trading Discord channel
  3. Sends a running P&L summary embed after all bets are placed

Results start as PENDING. After games finish you can resolve them:
  Manual:    python alerts/paper_trading.py resolve
  Automatic: python alerts/paper_trading.py auto_resolve
             (schedule this in Task Scheduler at 11:30pm ET)

Starting bankroll: $1,000 (set in .env as BANKROLL or defaults below)
"""

import os
import sys
import csv
import requests
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import DISCORD_WEBHOOK_PAPER, BANKROLL

TRADES_PATH     = os.path.join(os.path.dirname(__file__), '..', 'data', 'processed', 'paper_trades.csv')
STARTING_BANKROLL = float(BANKROLL) if BANKROLL else 1000.0

TRADE_COLUMNS = [
    'date', 'player', 'prop_type', 'side', 'line', 'odds',
    'ev', 'stake', 'bankroll_before', 'bankroll_after',
    'result', 'payout', 'net', 'matchup', 'book'
]


# ─────────────────────────────────────────────
# Bankroll state
# ─────────────────────────────────────────────

def get_current_bankroll() -> float:
    """
    Calculate current bankroll from trade history.
    Starts at STARTING_BANKROLL, adds payouts, subtracts stakes.
    """
    if not os.path.exists(TRADES_PATH):
        return STARTING_BANKROLL

    df = _load_trades()
    if df.empty:
        return STARTING_BANKROLL

    # Resolved bets: bankroll = starting + sum of net results
    resolved = df[df['result'].isin(['WIN', 'LOSS'])]
    net_resolved = resolved['net'].sum() if not resolved.empty else 0.0

    # Pending bets: stake is still committed (subtracted from bankroll)
    pending = df[df['result'] == 'PENDING']
    pending_stakes = pending['stake'].sum() if not pending.empty else 0.0

    return round(STARTING_BANKROLL + net_resolved - pending_stakes, 2)


def get_summary_stats() -> dict:
    """Return a full summary of all trades for the P&L embed."""
    if not os.path.exists(TRADES_PATH):
        return {
            'total_bets': 0, 'pending': 0, 'wins': 0, 'losses': 0,
            'total_staked': 0.0, 'total_payout': 0.0, 'net_pl': 0.0,
            'roi': 0.0, 'win_rate': 0.0, 'current_bankroll': STARTING_BANKROLL,
        }

    df = _load_trades()
    if df.empty:
        return {
            'total_bets': 0, 'pending': 0, 'wins': 0, 'losses': 0,
            'total_staked': 0.0, 'total_payout': 0.0, 'net_pl': 0.0,
            'roi': 0.0, 'win_rate': 0.0, 'current_bankroll': STARTING_BANKROLL,
        }

    resolved = df[df['result'].isin(['WIN', 'LOSS'])]
    wins     = df[df['result'] == 'WIN']
    losses   = df[df['result'] == 'LOSS']
    pending  = df[df['result'] == 'PENDING']

    total_staked  = resolved['stake'].sum() if not resolved.empty else 0.0
    total_payout  = resolved['payout'].sum() if not resolved.empty else 0.0
    net_pl        = total_payout - total_staked
    roi           = (net_pl / total_staked * 100) if total_staked > 0 else 0.0
    win_rate      = (len(wins) / len(resolved) * 100) if len(resolved) > 0 else 0.0

    return {
        'total_bets':       len(df),
        'pending':          len(pending),
        'wins':             len(wins),
        'losses':           len(losses),
        'total_staked':     round(total_staked, 2),
        'total_payout':     round(total_payout, 2),
        'net_pl':           round(net_pl, 2),
        'roi':              round(roi, 2),
        'win_rate':         round(win_rate, 1),
        'current_bankroll': get_current_bankroll(),
    }


# ─────────────────────────────────────────────
# Trade log helpers
# ─────────────────────────────────────────────

def _load_trades() -> pd.DataFrame:
    if not os.path.exists(TRADES_PATH):
        return pd.DataFrame(columns=TRADE_COLUMNS)
    df = pd.read_csv(TRADES_PATH)
    for col in ['stake', 'bankroll_before', 'bankroll_after', 'payout', 'net', 'ev']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0).astype(float)
    return df


def _save_trade(row: dict):
    os.makedirs(os.path.dirname(TRADES_PATH), exist_ok=True)
    file_exists = os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({col: row.get(col, '') for col in TRADE_COLUMNS})


# ─────────────────────────────────────────────
# Discord embeds
# ─────────────────────────────────────────────

def _send_embed(embed, label: str):
    from discord_webhook import DiscordWebhook
    url = DISCORD_WEBHOOK_PAPER
    if not url:
        print(f'  DISCORD_WEBHOOK_PAPER not set — skipping {label}')
        return False
    hook = DiscordWebhook(url=url, rate_limit_retry=True)
    hook.add_embed(embed)
    resp = hook.execute()
    return hasattr(resp, 'status_code') and resp.status_code in (200, 204)


def send_bet_placed(trade: dict, bankroll_after: float):
    """Send a 'BET PLACED' embed for a single paper trade."""
    from discord_webhook import DiscordEmbed

    ev         = float(trade.get('ev', 0))
    odds       = int(trade.get('odds', 0))
    stake      = float(trade.get('stake', 0))
    prop_type  = str(trade.get('prop_type', 'pitcher_strikeouts'))
    side       = trade.get('side', '')
    line       = trade.get('line', '')

    prop_label = (
        f'{side} {line} Ks' if prop_type == 'pitcher_strikeouts'
        else f'{side} {line} IP'
    )
    odds_str   = f'{odds:+d}'
    br_change  = bankroll_after - float(trade.get('bankroll_before', bankroll_after + stake))

    embed = DiscordEmbed(color=0x3498DB)
    embed.set_author(name='PAPER TRADE PLACED')
    embed.set_title(f"{trade['player']}  —  {prop_label}  {odds_str}")

    embed.add_embed_field(name='Stake',           value=f'**${stake:.2f}**',          inline=True)
    embed.add_embed_field(name='EV',              value=f'{ev:+.1%}',                 inline=True)
    embed.add_embed_field(name='Odds',            value=odds_str,                      inline=True)

    embed.add_embed_field(name='Bankroll Before', value=f'${trade["bankroll_before"]:.2f}', inline=True)
    embed.add_embed_field(name='Bankroll After',  value=f'${bankroll_after:.2f}',      inline=True)
    embed.add_embed_field(name='Matchup',         value=str(trade.get('matchup', 'N/A')), inline=True)

    embed.set_footer(text=f'Playbook Paper Trading  •  {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')
    embed.set_timestamp()

    _send_embed(embed, f'bet placed: {trade["player"]}')


def send_pl_summary():
    """Send a full P&L summary embed to the paper trading channel."""
    from discord_webhook import DiscordEmbed

    stats = get_summary_stats()
    pl    = stats['net_pl']
    roi   = stats['roi']

    # Color: green if profitable, red if losing, grey if no resolved bets
    if stats['wins'] + stats['losses'] == 0:
        color = 0x95A5A6   # grey — nothing resolved yet
    elif pl >= 0:
        color = 0x2ECC71   # green
    else:
        color = 0xE74C3C   # red

    pl_str  = f'**+${pl:.2f}**' if pl >= 0 else f'**-${abs(pl):.2f}**'
    roi_str = f'{roi:+.1f}%'

    # Bankroll bar — visual representation of progress
    pct     = stats['current_bankroll'] / STARTING_BANKROLL
    filled  = round(min(pct, 2.0) * 5)   # 0-10 blocks, capped at 200%
    bar     = 'X' * filled + 'o' * (10 - min(filled, 10))
    bar_str = f'[{bar}]  ${stats["current_bankroll"]:.2f}'

    embed = DiscordEmbed(color=color)
    embed.set_author(name='PAPER TRADING — P&L SUMMARY')
    embed.set_title('Bankroll Tracker')
    embed.set_description(f'```\n{bar_str}\n```')

    embed.add_embed_field(name='Starting Bankroll', value=f'${STARTING_BANKROLL:.2f}',           inline=True)
    embed.add_embed_field(name='Current Bankroll',  value=f'**${stats["current_bankroll"]:.2f}**', inline=True)
    embed.add_embed_field(name='Net P&L',           value=pl_str,                                  inline=True)

    embed.add_embed_field(name='Total Bets',        value=str(stats['total_bets']),               inline=True)
    embed.add_embed_field(name='Pending',           value=str(stats['pending']),                  inline=True)
    embed.add_embed_field(name='ROI',               value=roi_str,                                inline=True)

    embed.add_embed_field(name='Wins',              value=str(stats['wins']),                     inline=True)
    embed.add_embed_field(name='Losses',            value=str(stats['losses']),                   inline=True)
    embed.add_embed_field(name='Win Rate',          value=f'{stats["win_rate"]:.1f}%' if stats['wins'] + stats['losses'] > 0 else 'N/A', inline=True)

    embed.add_embed_field(name='Total Staked',      value=f'${stats["total_staked"]:.2f}',        inline=True)
    embed.add_embed_field(name='Total Returned',    value=f'${stats["total_payout"]:.2f}',        inline=True)
    embed.add_embed_field(
        name='Status',
        value='Awaiting results' if stats['pending'] > 0 else ('Profitable' if pl >= 0 else 'In the red'),
        inline=True
    )

    embed.set_footer(text=f'Playbook Paper Trading  •  {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')
    embed.set_timestamp()

    _send_embed(embed, 'P&L summary')


# ─────────────────────────────────────────────
# Main entry — called by ev_calculator
# ─────────────────────────────────────────────

def log_bets_from_signals(signals_df: pd.DataFrame,
                           ev_threshold: float = 0.04,
                           max_bets: int = 5):
    """
    Log the top EV signals as paper trades and fire Discord embeds.
    Called automatically after EV calculation.
    """
    flagged = signals_df[signals_df['ev'] >= ev_threshold].sort_values('ev', ascending=False)
    if flagged.empty:
        print('  No signals to paper trade.')
        return

    cap     = min(len(flagged), max_bets)
    bankroll = get_current_bankroll()
    placed  = 0

    print(f'  Logging {cap} paper trade(s) — bankroll: ${bankroll:.2f}')

    for _, row in flagged.head(cap).iterrows():
        stake          = float(row.get('kelly_dollars', 0))
        bankroll_before = bankroll
        bankroll_after  = round(bankroll - stake, 2)   # stake is committed

        trade = {
            'date':            datetime.now().strftime('%Y-%m-%d %H:%M'),
            'player':          row.get('player', ''),
            'prop_type':       row.get('prop_type', 'pitcher_strikeouts'),
            'side':            row.get('side', ''),
            'line':            row.get('line', ''),
            'odds':            int(row.get('odds', 0)),
            'ev':              round(float(row.get('ev', 0)), 4),
            'stake':           round(stake, 2),
            'bankroll_before': round(bankroll_before, 2),
            'bankroll_after':  round(bankroll_after, 2),
            'result':          'PENDING',
            'payout':          0.0,
            'net':             0.0,
            'matchup':         row.get('matchup', ''),
            'book':            row.get('book', ''),
        }

        _save_trade(trade)
        send_bet_placed(trade, bankroll_after)

        try:
            from database import log_paper_trade
            log_paper_trade(trade)
        except Exception as e:
            print(f'    Supabase log error: {e}')

        bankroll = bankroll_after
        placed += 1

        print(f'    Logged: {trade["player"]}  {trade["side"]}  {trade["line"]}  '
              f'${stake:.2f}  (bankroll: ${bankroll:.2f})')

    # Send P&L summary after all bets
    send_pl_summary()
    print(f'  P&L summary sent. Current bankroll: ${bankroll:.2f}')


# ─────────────────────────────────────────────
# Auto-resolve — MLB Stats API box score lookup
# ─────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Lowercase, strip whitespace, drop Jr/Sr/III suffixes."""
    if not isinstance(name, str):
        return ''
    name = name.lower().strip()
    for suffix in [' jr.', ' sr.', ' jr', ' sr', ' iii', ' ii', ' iv']:
        name = name.replace(suffix, '')
    return name.strip()


def _fetch_pitcher_results(date: str) -> dict:
    """
    Hit the MLB Stats API and return actual results for every starting pitcher
    whose game is Final on the given date.

    Returns: { normalized_name: {'ks': int, 'ip': float} }

    IP is converted from MLB's "outs" format (e.g. '6.2' = 6 2/3 innings = 6.667)
    so it can be directly compared against a prop line like 5.5.
    """
    results = {}
    try:
        sched = requests.get(
            f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}',
            timeout=8
        ).json()

        games = sched.get('dates', [{}])[0].get('games', [])

        for game in games:
            if game.get('status', {}).get('abstractGameState') != 'Final':
                continue  # game not finished yet — skip

            gid = game['gamePk']
            try:
                bs = requests.get(
                    f'https://statsapi.mlb.com/api/v1/game/{gid}/boxscore',
                    timeout=8
                ).json()

                for side in ['away', 'home']:
                    pitchers = bs['teams'][side]['pitchers']
                    players  = bs['teams'][side]['players']
                    if not pitchers:
                        continue

                    sp_id    = f'ID{pitchers[0]}'
                    sp       = players.get(sp_id, {})
                    name     = sp.get('person', {}).get('fullName', '')
                    pitching = sp.get('stats', {}).get('pitching', {})
                    ks       = pitching.get('strikeOuts')

                    # Convert MLB innings string: '6.2' means 6 full innings + 2 outs
                    # = 6 + 2/3 = 6.667 real innings
                    ip = None
                    ip_str = str(pitching.get('inningsPitched', ''))
                    if ip_str:
                        try:
                            parts = ip_str.split('.')
                            ip = int(parts[0]) + (int(parts[1]) / 3 if len(parts) > 1 and parts[1] else 0)
                        except (ValueError, IndexError):
                            pass

                    if name:
                        results[_normalize(name)] = {'ks': ks, 'ip': ip}

            except Exception:
                continue  # bad box score — skip this game

    except Exception as e:
        print(f'  Warning: could not fetch results for {date}: {e}')

    return results


def _match_pitcher(player: str, results: dict):
    """
    Find a pitcher's result dict from the box score lookup.
    Tries exact normalized match first, then last-name fallback.
    Returns the result dict or None if not found.
    """
    norm = _normalize(player)
    if norm in results:
        return results[norm]

    last = norm.split()[-1] if norm else ''
    matches = {k: v for k, v in results.items() if k.split()[-1] == last}
    if len(matches) == 1:
        return list(matches.values())[0]

    return None


def auto_resolve():
    """
    Automatically resolve PENDING bets using MLB Stats API box scores.

    For each pending bet:
      - Looks up the pitcher's actual K total (or IP) from the final box score
      - Marks WIN or LOSS based on Over/Under vs the line
      - Calculates payout, saves to CSV, fires a P&L update to Discord

    Safe to run multiple times — skips bets whose game is not yet Final.
    Schedule in Task Scheduler at 11:30pm ET to run nightly.

    Run with:  python alerts/paper_trading.py auto_resolve
    """
    print('=' * 55)
    print('  PLAYBOOK -- Auto-Resolve Paper Trades')
    print('=' * 55)

    # Load pending trades — Supabase first (works on Railway where CSV doesn't
    # persist), fall back to local CSV for Windows use.
    pending_rows = []
    source       = 'csv'

    try:
        from database import get_pending_trades
        db_rows = get_pending_trades()
        if db_rows:
            pending_rows = db_rows
            source       = 'supabase'
            print(f'\nLoaded {len(pending_rows)} pending trade(s) from Supabase.')
    except Exception as e:
        print(f'  Supabase unavailable ({e}) — falling back to CSV.')

    if not pending_rows:
        df      = _load_trades()
        pending = df[df['result'] == 'PENDING']
        if pending.empty:
            print('No pending bets to resolve.')
            return
        pending_rows = pending.to_dict('records')
        source       = 'csv'
        print(f'\nLoaded {len(pending_rows)} pending trade(s) from CSV.')

    date_key = 'trade_date' if source == 'supabase' else 'date'

    # ── Remove stale pending trades (from a previous day) ────────────────────
    # A trade that is still PENDING the morning after its game date will never
    # resolve cleanly. Drop it now so it doesn't pollute the record.
    today_str   = datetime.now().strftime('%Y-%m-%d')
    stale_rows  = [r for r in pending_rows if str(r[date_key])[:10] < today_str]
    pending_rows = [r for r in pending_rows if str(r[date_key])[:10] >= today_str]

    if stale_rows:
        print(f'\nRemoving {len(stale_rows)} stale pending trade(s) from previous day(s):')
        for r in stale_rows:
            print(f'  {r["player"]}  ({str(r[date_key])[:10]})')
            try:
                from database import update_paper_trade_result
                update_paper_trade_result(
                    r['player'], str(r[date_key])[:10],
                    r['side'], float(r['line']),
                    'EXPIRED', 0.0, 0.0
                )
            except Exception as e:
                print(f'    Supabase remove error: {e}')

        # Remove from local CSV
        df = _load_trades() if os.path.exists(TRADES_PATH) else None
        if df is not None:
            for r in stale_rows:
                mask = (
                    (df['player'] == r['player']) &
                    (df['result'] == 'PENDING') &
                    (df['date'].astype(str).str[:10] < today_str)
                )
                df = df[~mask]
            df.to_csv(TRADES_PATH, index=False)
            print(f'  Stale trades removed from CSV.')

    if not pending_rows:
        print('\nNo active pending bets to resolve.')
        # Still send heartbeat so health channel knows the bot ran
        try:
            from alerts.discord_alerts import send_heartbeat
            stats = get_summary_stats()
            send_heartbeat(game_count=0, pending_trades=0,
                           wins=stats['wins'], losses=stats['losses'])
        except Exception as e:
            print(f'  Heartbeat error: {e}')
        return

    print(f'\nFetching box scores...\n')

    # Fetch box scores for every unique bet date
    dates = list({str(r[date_key])[:10] for r in pending_rows})
    box_scores = {}
    for date in dates:
        box_scores[date] = _fetch_pitcher_results(date)
        print(f'  {date}: box scores for {len(box_scores[date])} starting pitchers')

    print()
    updated = False
    skipped = []

    # Also load the CSV df so we can keep it in sync if it exists locally
    df = _load_trades() if os.path.exists(TRADES_PATH) else None

    for row in pending_rows:
        date_val  = str(row[date_key])[:10]
        player    = row['player']
        side      = row['side']
        line      = float(row['line'])
        prop_type = str(row.get('prop_type', 'pitcher_strikeouts'))
        stake     = float(row['stake'])
        odds      = float(row['odds'])

        stats = _match_pitcher(player, box_scores.get(date_val, {}))

        if stats is None:
            skipped.append(player)
            continue

        actual = stats['ip'] if prop_type == 'pitcher_innings' else stats['ks']

        if actual is None:
            skipped.append(player)
            continue

        won = (side == 'Over' and actual > line) or (side == 'Under' and actual < line)

        if won:
            decimal = _american_to_decimal(odds)
            payout  = round(stake * decimal, 2)
            net     = round(payout - stake, 2)
            result  = 'WIN'
            print(f'  WIN   {player:<24} {side} {line}  actual: {actual}  net: +${net:.2f}')
        else:
            payout = 0.0
            net    = -stake
            result = 'LOSS'
            print(f'  LOSS  {player:<24} {side} {line}  actual: {actual}  net: -${stake:.2f}')

        # Update Supabase
        try:
            from database import update_paper_trade_result
            update_paper_trade_result(player, date_val, side, line, result, payout, net)
        except Exception as e:
            print(f'    Supabase update error: {e}')

        # Keep local CSV in sync if it exists
        if df is not None:
            mask = (
                (df['player'] == player) &
                (df['side'] == side) &
                (df['line'].astype(float) == line) &
                (df['result'] == 'PENDING')
            )
            df.loc[mask, ['result', 'payout', 'net']] = [result, payout, net]

        updated = True

    if skipped:
        print(f'\n  Still pending ({len(skipped)} — game not finished or pitcher not in box score):')
        for p in skipped:
            print(f'    {p}')

    if updated:
        if df is not None:
            df.to_csv(TRADES_PATH, index=False)
            print('\nResults saved to paper_trades.csv')
        print('Sending updated P&L to Discord...')
        send_pl_summary()
        print('Done.')
    else:
        print('\nNo bets resolved — all games may still be in progress.')

    # Send daily heartbeat to health channel
    try:
        from alerts.discord_alerts import send_heartbeat
        stats      = get_summary_stats()
        game_count = sum(len(v) for v in box_scores.values())
        send_heartbeat(
            game_count=game_count,
            pending_trades=len(skipped),
            wins=stats['wins'],
            losses=stats['losses'],
        )
    except Exception as e:
        print(f'  Heartbeat error: {e}')


# ─────────────────────────────────────────────
# Resolve pending bets (mark WIN or LOSS)
# ─────────────────────────────────────────────

def _american_to_decimal(american_odds: float) -> float:
    """Convert American odds to decimal (used for payout calculation)."""
    if american_odds >= 0:
        return (american_odds / 100) + 1
    else:
        return (100 / abs(american_odds)) + 1


def resolve_pending():
    """
    Walk through every PENDING bet and let you mark each one WIN or LOSS.
    Calculates the payout, updates the CSV, then sends a fresh P&L to Discord.

    Run with:  python alerts/paper_trading.py resolve
    """
    df = _load_trades()
    pending = df[df['result'] == 'PENDING']

    if pending.empty:
        print('No pending bets to resolve.')
        return

    print(f'\nFound {len(pending)} pending bet(s):')
    print()

    updated = False

    for idx, row in pending.iterrows():
        print(
            f"  {row['date']}  |  {row['player']}  "
            f"{row['side']} {row['line']} Ks  "
            f"odds: {int(row['odds']):+d}  stake: ${float(row['stake']):.2f}"
        )

        while True:
            answer = input('    Result? (W = win,  L = loss,  S = skip): ').strip().upper()
            if answer in ('W', 'L', 'S'):
                break
            print('    Please type W, L, or S.')

        if answer == 'S':
            print('    Skipped.\n')
            continue

        if answer == 'W':
            decimal = _american_to_decimal(float(row['odds']))
            payout  = round(float(row['stake']) * decimal, 2)
            net     = round(payout - float(row['stake']), 2)
            df.at[idx, 'result'] = 'WIN'
            df.at[idx, 'payout'] = payout
            df.at[idx, 'net']    = net
            print(f'    WIN  —  returned ${payout:.2f}  (net +${net:.2f})\n')
        else:
            payout = 0.0
            net    = -float(row['stake'])
            df.at[idx, 'result'] = 'LOSS'
            df.at[idx, 'payout'] = payout
            df.at[idx, 'net']    = net
            print(f'    LOSS  —  net -${float(row["stake"]):.2f}\n')

        try:
            from database import update_paper_trade_result
            update_paper_trade_result(
                row['player'], str(row['date'])[:10], row['side'],
                float(row['line']), df.at[idx, 'result'],
                df.at[idx, 'payout'], df.at[idx, 'net']
            )
        except Exception as e:
            print(f'    Supabase update error: {e}')

        updated = True

    if updated:
        df.to_csv(TRADES_PATH, index=False)
        print('Results saved to paper_trades.csv')
        print('Sending updated P&L summary to Discord...')
        send_pl_summary()
        print('Done.')
    else:
        print('No bets were updated.')


# ─────────────────────────────────────────────
# Standalone: run to send a fresh P&L summary
# ─────────────────────────────────────────────

def run():
    print('=' * 55)
    print('  PLAYBOOK -- Paper Trading P&L')
    print('=' * 55)

    stats = get_summary_stats()
    print(f'  Starting bankroll:  ${STARTING_BANKROLL:.2f}')
    print(f'  Current bankroll:   ${stats["current_bankroll"]:.2f}')
    print(f'  Net P&L:            ${stats["net_pl"]:+.2f}')
    print(f'  Total bets:         {stats["total_bets"]}')
    print(f'  Pending:            {stats["pending"]}')
    print(f'  Wins / Losses:      {stats["wins"]} / {stats["losses"]}')
    print(f'  ROI:                {stats["roi"]:+.1f}%')

    print('\nSending P&L summary to Discord...')
    ok = send_pl_summary()
    if ok is not False:
        print('  Sent.')
    else:
        print('  Failed — check DISCORD_WEBHOOK_PAPER in .env')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'resolve':
        resolve_pending()
    elif len(sys.argv) > 1 and sys.argv[1] == 'auto_resolve':
        auto_resolve()
    else:
        run()

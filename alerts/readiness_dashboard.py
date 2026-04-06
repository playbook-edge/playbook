"""
alerts/readiness_dashboard.py — Weekly real-money readiness dashboard.

Answers one question: is the model good enough to bet real money?

Three signals it checks:
  1. CLV (Closing Line Value) — are we consistently getting better prices
     than where the market settles? Positive avg CLV = model finds real edges.
  2. ROI — are we actually profiting on resolved bets?
  3. Win rate vs expected — are we winning at the rate our model predicts?

Verdict thresholds (requires 30+ resolved bets for meaningful sample):
  GO         avg CLV > 0%  AND  ROI > 0%   AND  30+ bets resolved
  MONITOR    avg CLV > 0%  BUT  ROI < 0%   (edges real, variance hurting)
  NOT YET    fewer than 30 resolved bets
  HOLD       avg CLV < 0%  (model not finding real edges)

Usage:
    python alerts/readiness_dashboard.py          # send to Discord
    python alerts/readiness_dashboard.py preview  # print to terminal only

Scheduled weekly: Railway cron service at 9am ET Sundays (0 13 * * 0)
"""

import os
import sys
import math
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import DISCORD_WEBHOOK_HEALTH, DISCORD_WEBHOOK_PAPER, BANKROLL

TRADES_PATH     = os.path.join(os.path.dirname(__file__), '..', 'data', 'processed', 'paper_trades.csv')
STARTING_BANKROLL = float(BANKROLL) if BANKROLL else 1000.0
MIN_BETS_FOR_VERDICT = 30


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _american_to_decimal(odds: float) -> float:
    if odds >= 0:
        return (odds / 100) + 1
    return (100 / abs(odds)) + 1


def _breakeven_win_rate(odds: float) -> float:
    """Minimum win rate needed to break even at these odds."""
    dec = _american_to_decimal(odds)
    return round(1 / dec, 4)


def _tier(ev: float) -> str:
    if ev >= 0.20: return 'DEGEN'
    if ev >= 0.12: return 'AGGRESSIVE'
    if ev >= 0.07: return 'MODERATE'
    return 'CONSERVATIVE'


def _tier_emoji(tier: str) -> str:
    return {'CONSERVATIVE': '🟢', 'MODERATE': '🟡',
            'AGGRESSIVE': '🔴', 'DEGEN': '🟣'}.get(tier, '')


# ─────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────

def _load_trades() -> pd.DataFrame:
    """Load paper_trades.csv, or pull from Supabase if CSV missing."""
    if os.path.exists(TRADES_PATH):
        df = pd.read_csv(TRADES_PATH)
    else:
        # On Railway the CSV doesn't persist — fall back to Supabase
        try:
            from database import get_client
            client = get_client()
            resp = client.table('paper_trades').select('*').execute()
            df = pd.DataFrame(resp.data or [])
            # Supabase uses trade_date; normalise to 'date'
            if 'trade_date' in df.columns and 'date' not in df.columns:
                df = df.rename(columns={'trade_date': 'date'})
        except Exception as e:
            print(f'  Could not load trades from Supabase: {e}')
            return pd.DataFrame()

    if df.empty:
        return df

    for col in ['stake', 'payout', 'net', 'ev', 'odds']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

    return df


def _load_clv() -> pd.DataFrame:
    """Load closing_lines from Supabase."""
    try:
        from database import get_client
        client = get_client()
        resp = client.table('closing_lines').select('*').execute()
        df = pd.DataFrame(resp.data or [])
        if not df.empty:
            df['clv_pct'] = pd.to_numeric(df['clv_pct'], errors='coerce')
        return df
    except Exception as e:
        print(f'  Could not load CLV data: {e}')
        return pd.DataFrame()


# ─────────────────────────────────────────────
# Stats computation
# ─────────────────────────────────────────────

def compute_readiness_stats() -> dict:
    """
    Compute all stats needed for the dashboard.
    Returns a dict with overall stats + per-tier breakdown.
    """
    trades = _load_trades()
    clv_df = _load_clv()

    # ── Overall resolved trades ───────────────
    resolved = trades[trades['result'].isin(['WIN', 'LOSS'])].copy() if not trades.empty else pd.DataFrame()
    pending  = trades[trades['result'] == 'PENDING'].copy() if not trades.empty else pd.DataFrame()

    total_resolved = len(resolved)
    total_pending  = len(pending)
    wins           = int((resolved['result'] == 'WIN').sum())  if not resolved.empty else 0
    losses         = int((resolved['result'] == 'LOSS').sum()) if not resolved.empty else 0
    win_rate       = wins / total_resolved if total_resolved > 0 else None

    total_staked   = float(resolved['stake'].sum())  if not resolved.empty else 0.0
    total_returned = float(resolved['payout'].sum()) if not resolved.empty else 0.0
    net_pl         = total_returned - total_staked
    roi            = net_pl / total_staked if total_staked > 0 else None

    current_bankroll = STARTING_BANKROLL + net_pl - float(pending['stake'].sum() if not pending.empty else 0.0)

    # ── Expected win rate from odds ───────────
    # Compare actual win rate to what the book's odds implied.
    # If we're winning more than break-even rate → real edge.
    expected_win_rate = None
    if not resolved.empty and 'odds' in resolved.columns:
        breakevens = resolved['odds'].apply(_breakeven_win_rate)
        expected_win_rate = float(breakevens.mean())

    # ── CLV summary ───────────────────────────
    avg_clv      = None
    clv_positive = None    # % of bets where CLV > 0
    clv_count    = 0

    if not clv_df.empty and 'clv_pct' in clv_df.columns:
        valid_clv = clv_df['clv_pct'].dropna()
        if not valid_clv.empty:
            avg_clv      = float(valid_clv.mean())
            clv_positive = float((valid_clv > 0).sum() / len(valid_clv))
            clv_count    = len(valid_clv)

    # ── Per-tier breakdown ────────────────────
    tier_stats = {}
    if not resolved.empty and 'ev' in resolved.columns:
        resolved = resolved.copy()
        resolved['tier'] = resolved['ev'].apply(_tier)
        for tier in ['CONSERVATIVE', 'MODERATE', 'AGGRESSIVE', 'DEGEN']:
            t = resolved[resolved['tier'] == tier]
            if t.empty:
                continue
            t_wins    = int((t['result'] == 'WIN').sum())
            t_losses  = int((t['result'] == 'LOSS').sum())
            t_total   = t_wins + t_losses
            t_staked  = float(t['stake'].sum())
            t_net     = float(t['net'].sum())
            t_roi     = t_net / t_staked if t_staked > 0 else None
            t_wr      = t_wins / t_total if t_total > 0 else None
            # CLV for this tier — match by player + date + side + line
            t_clv     = None
            if not clv_df.empty:
                # Join on player + line + side (date may differ slightly)
                merged = t.merge(clv_df[['player', 'line', 'side', 'clv_pct']],
                                 on=['player', 'line', 'side'], how='left')
                valid  = merged['clv_pct'].dropna()
                if not valid.empty:
                    t_clv = float(valid.mean())

            tier_stats[tier] = {
                'total': t_total, 'wins': t_wins, 'losses': t_losses,
                'win_rate': t_wr, 'roi': t_roi, 'avg_clv': t_clv,
            }

    # ── Readiness verdict ─────────────────────
    if total_resolved < MIN_BETS_FOR_VERDICT:
        verdict = 'NOT YET'
        verdict_reason = f'Need {MIN_BETS_FOR_VERDICT} resolved bets — have {total_resolved}'
    elif avg_clv is not None and avg_clv > 0 and roi is not None and roi > 0:
        verdict = 'GO'
        verdict_reason = 'Positive CLV + positive ROI — model finding real edges'
    elif avg_clv is not None and avg_clv > 0 and roi is not None and roi <= 0:
        verdict = 'MONITOR'
        verdict_reason = 'Positive CLV but negative ROI — edges are real, variance hurting'
    elif avg_clv is not None and avg_clv <= 0:
        verdict = 'HOLD'
        verdict_reason = 'Negative avg CLV — model not consistently beating the close'
    else:
        verdict = 'NOT YET'
        verdict_reason = f'Insufficient data ({total_resolved} bets resolved)'

    return {
        # Overall
        'total_resolved':      total_resolved,
        'total_pending':       total_pending,
        'wins':                wins,
        'losses':              losses,
        'win_rate':            win_rate,
        'expected_win_rate':   expected_win_rate,
        'total_staked':        total_staked,
        'net_pl':              net_pl,
        'roi':                 roi,
        'current_bankroll':    current_bankroll,
        # CLV
        'avg_clv':             avg_clv,
        'clv_positive_rate':   clv_positive,
        'clv_count':           clv_count,
        # Tiers
        'tier_stats':          tier_stats,
        # Verdict
        'verdict':             verdict,
        'verdict_reason':      verdict_reason,
    }


# ─────────────────────────────────────────────
# Terminal preview
# ─────────────────────────────────────────────

def print_dashboard(s: dict):
    print()
    print('=' * 58)
    print('  PLAYBOOK - REAL-MONEY READINESS DASHBOARD')
    print('=' * 58)

    verdict_icons = {'GO': '[GO]', 'MONITOR': '[MONITOR]', 'NOT YET': '[NOT YET]', 'HOLD': '[HOLD]'}
    print(f"\n  VERDICT: {verdict_icons.get(s['verdict'], '')} {s['verdict']}")
    print(f"  {s['verdict_reason']}")

    print(f"\n  {'-'*50}")
    print(f"  OVERALL  ({s['total_resolved']} resolved, {s['total_pending']} pending)")
    print(f"  {'-'*50}")
    wr_str  = f"{s['win_rate']:.1%}"  if s['win_rate']  is not None else 'N/A'
    exp_str = f"{s['expected_win_rate']:.1%}" if s['expected_win_rate'] is not None else 'N/A'
    roi_str = f"{s['roi']:+.1%}"     if s['roi']        is not None else 'N/A'
    pl_str  = f"{'+'if s['net_pl']>=0 else ''}${s['net_pl']:.2f}"
    print(f"  Win rate:         {wr_str}  (break-even: {exp_str})")
    print(f"  ROI:              {roi_str}  |  Net P&L: {pl_str}")
    print(f"  Bankroll:         ${s['current_bankroll']:.2f}  (started ${STARTING_BANKROLL:.2f})")

    clv_str  = f"{s['avg_clv']:+.2%}" if s['avg_clv'] is not None else 'N/A'
    clvp_str = f"{s['clv_positive_rate']:.0%}" if s['clv_positive_rate'] is not None else 'N/A'
    print(f"\n  AVG CLV:          {clv_str}  ({clvp_str} of bets beat the close, n={s['clv_count']})")

    if s['tier_stats']:
        print(f"\n  {'-'*50}")
        print(f"  BY TIER")
        print(f"  {'-'*50}")
        print(f"  {'Tier':<14} {'Bets':>5} {'W/L':>7} {'Win%':>7} {'ROI':>8} {'Avg CLV':>9}")
        tier_labels = {'CONSERVATIVE': '[G]', 'MODERATE': '[Y]', 'AGGRESSIVE': '[R]', 'DEGEN': '[P]'}
        for tier in ['CONSERVATIVE', 'MODERATE', 'AGGRESSIVE', 'DEGEN']:
            t = s['tier_stats'].get(tier)
            if not t:
                continue
            wr   = f"{t['win_rate']:.0%}"  if t['win_rate']  is not None else 'N/A'
            roi  = f"{t['roi']:+.1%}"      if t['roi']        is not None else 'N/A'
            clv  = f"{t['avg_clv']:+.2%}"  if t['avg_clv']    is not None else 'N/A'
            wl   = f"{t['wins']}/{t['losses']}"
            lbl  = tier_labels.get(tier, '')
            print(f"  {lbl} {tier:<12} {t['total']:>5} {wl:>7} {wr:>7} {roi:>8} {clv:>9}")

    print()


# ─────────────────────────────────────────────
# Discord embed
# ─────────────────────────────────────────────

def send_dashboard(s: dict):
    """Send the readiness dashboard as a Discord embed to the health channel."""
    try:
        from discord_webhook import DiscordWebhook, DiscordEmbed
    except ImportError:
        print('  discord_webhook not installed — skipping Discord send.')
        return

    url = DISCORD_WEBHOOK_HEALTH
    if not url:
        print('  DISCORD_WEBHOOK_HEALTH not set — skipping Discord send.')
        return

    # Verdict colors
    verdict_colors = {
        'GO':       0x2ECC71,   # green
        'MONITOR':  0xF1C40F,   # yellow
        'NOT YET':  0x95A5A6,   # grey
        'HOLD':     0xE74C3C,   # red
    }
    verdict_icons = {'GO': '✅', 'MONITOR': '⚠️', 'NOT YET': '⏳', 'HOLD': '🛑'}

    color  = verdict_colors.get(s['verdict'], 0x95A5A6)
    icon   = verdict_icons.get(s['verdict'], '')
    now    = datetime.now().strftime('%b %d, %Y')

    embed = DiscordEmbed(color=color)
    embed.set_author(name='PLAYBOOK — REAL-MONEY READINESS')
    embed.set_title(f"{icon}  Verdict: {s['verdict']}")
    embed.set_description(f"*{s['verdict_reason']}*")

    # Row 1 — sample size
    embed.add_embed_field(
        name='Resolved Bets',
        value=f"**{s['total_resolved']}**  ({s['total_pending']} pending)",
        inline=True
    )
    embed.add_embed_field(
        name='W / L',
        value=f"{s['wins']} / {s['losses']}",
        inline=True
    )
    wr_str  = f"{s['win_rate']:.1%}"          if s['win_rate']          is not None else 'N/A'
    exp_str = f"{s['expected_win_rate']:.1%}" if s['expected_win_rate'] is not None else 'N/A'
    embed.add_embed_field(
        name='Win Rate',
        value=f"**{wr_str}**  (need {exp_str})",
        inline=True
    )

    # Row 2 — money
    roi_str = f"{s['roi']:+.1%}" if s['roi'] is not None else 'N/A'
    pl_str  = (f"+${s['net_pl']:.2f}" if s['net_pl'] >= 0
               else f"-${abs(s['net_pl']):.2f}")
    embed.add_embed_field(name='ROI',           value=f"**{roi_str}**",          inline=True)
    embed.add_embed_field(name='Net P&L',       value=pl_str,                    inline=True)
    embed.add_embed_field(name='Bankroll',       value=f"${s['current_bankroll']:.2f}", inline=True)

    # Row 3 — CLV (the most important signal)
    clv_str  = f"{s['avg_clv']:+.2%}" if s['avg_clv'] is not None else 'N/A'
    clvp_str = f"{s['clv_positive_rate']:.0%}" if s['clv_positive_rate'] is not None else 'N/A'
    clv_label = ('🟢 Beating the close' if s['avg_clv'] is not None and s['avg_clv'] > 0
                 else '🔴 Not beating the close' if s['avg_clv'] is not None
                 else 'No CLV data yet')
    embed.add_embed_field(
        name='Avg CLV',
        value=f"**{clv_str}**  {clv_label}",
        inline=True
    )
    embed.add_embed_field(
        name='Beat the Close',
        value=f"{clvp_str} of bets  (n={s['clv_count']})",
        inline=True
    )
    embed.add_embed_field(name='\u200b', value='\u200b', inline=True)  # spacer

    # Tier breakdown
    if s['tier_stats']:
        tier_lines = []
        for tier in ['CONSERVATIVE', 'MODERATE', 'AGGRESSIVE', 'DEGEN']:
            t = s['tier_stats'].get(tier)
            if not t:
                continue
            em   = _tier_emoji(tier)
            wr   = f"{t['win_rate']:.0%}" if t['win_rate'] is not None else '—'
            roi  = f"{t['roi']:+.1%}"     if t['roi']      is not None else '—'
            clv  = f"{t['avg_clv']:+.2%}" if t['avg_clv']  is not None else '—'
            tier_lines.append(
                f"{em} **{tier}** ({t['total']} bets)  "
                f"WR: {wr}  ROI: {roi}  CLV: {clv}"
            )
        embed.add_embed_field(
            name='By Tier',
            value='\n'.join(tier_lines) if tier_lines else 'No resolved bets by tier yet.',
            inline=False
        )

    embed.set_footer(text=f'Playbook Weekly Readiness  •  {now}')
    embed.set_timestamp()

    hook = DiscordWebhook(url=url, rate_limit_retry=True)
    hook.add_embed(embed)
    resp = hook.execute()

    if hasattr(resp, 'status_code') and resp.status_code in (200, 204):
        print('  Readiness dashboard sent to Discord.')
    else:
        print(f'  Discord send failed: {resp}')


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def run(preview_only: bool = False):
    print('Computing readiness stats...')
    stats = compute_readiness_stats()
    print_dashboard(stats)

    if not preview_only:
        send_dashboard(stats)
    else:
        print('  (preview only — not sent to Discord)')


if __name__ == '__main__':
    preview = len(sys.argv) > 1 and sys.argv[1] == 'preview'
    run(preview_only=preview)

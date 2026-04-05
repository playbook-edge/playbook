"""
alerts/discord_alerts.py

Sends formatted Discord embed alerts for Playbook EV signals.

Tiers by EV:
  Conservative  4–7%    green   safe, high-confidence plays
  Moderate      7–12%   yellow  solid edge, normal risk
  Aggressive   12–20%   red     strong edge, higher variance
  Degen         20%+    purple  extreme edge (small sample warning)

PlaybookIQ is a 0-100 composite score combining EV, edge,
pitcher quality (xFIP), and sample size (IP per start).
"""

import os
import sys
import requests
import pandas as pd
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import DISCORD_WEBHOOK_CONSERVATIVE

# Tier definitions — (min_ev, label, color_hex, emoji)
TIERS = [
    (0.20, 'DEGEN',        0x9B59B6, '🟣'),
    (0.12, 'AGGRESSIVE',   0xE74C3C, '🔴'),
    (0.07, 'MODERATE',     0xF1C40F, '🟡'),
    (0.04, 'CONSERVATIVE', 0x2ECC71, '🟢'),
]


# ─────────────────────────────────────────────
# PlaybookIQ composite score (0–100)
# ─────────────────────────────────────────────

def calculate_playbook_iq(ev: float, edge: float,
                           xfip: float = None,
                           ip_per_start: float = None) -> int:
    """
    Composite confidence score. Four components:
      EV quality      0–40 pts   (20% EV = max)
      Edge size       0–30 pts   (15% edge = max)
      Pitcher quality 0–20 pts   (xFIP 1.0 = max, 5.0 = 0)
      Sample size     0–10 pts   (6+ IP/start = max)
    """
    ev_score = min(abs(ev) / 0.20, 1.0) * 40

    edge_score = min(abs(edge) / 0.15, 1.0) * 30

    if xfip is not None and not pd.isna(xfip):
        xfip_score = max(0.0, (5.0 - float(xfip)) / 4.0) * 20
    else:
        xfip_score = 10.0   # neutral when unknown

    if ip_per_start is not None and not pd.isna(ip_per_start):
        sample_score = min(float(ip_per_start) / 6.0, 1.0) * 10
    else:
        sample_score = 5.0  # neutral

    total = ev_score + edge_score + xfip_score + sample_score
    return int(round(min(max(total, 0), 100)))


def iq_bar(score: int) -> str:
    """Visual bar for the IQ score. e.g.  ████████░░  82"""
    filled = round(score / 10)
    empty  = 10 - filled
    return '█' * filled + '░' * empty + f'  {score}/100'


# ─────────────────────────────────────────────
# Tier logic
# ─────────────────────────────────────────────

def get_tier(ev: float) -> tuple:
    """Return (label, color_hex, emoji) for a given EV."""
    for threshold, label, color, emoji in TIERS:
        if ev >= threshold:
            return label, color, emoji
    return 'CONSERVATIVE', 0x2ECC71, '🟢'


# ─────────────────────────────────────────────
# Game time lookup (MLB Stats API)
# ─────────────────────────────────────────────

def get_game_time(matchup: str) -> str:
    """
    Look up today's game time for a matchup string like 'Marlins @ Yankees'.
    Returns a formatted string like '7:05 PM ET' or 'TBD'.
    """
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        url   = (
            f'https://statsapi.mlb.com/api/v1/schedule'
            f'?sportId=1&date={today}&hydrate=team'
        )
        data  = requests.get(url, timeout=8).json()

        # Extract team names from matchup string
        parts = matchup.replace(' @ ', '|').split('|')
        if len(parts) != 2:
            return 'TBD'
        away_frag, home_frag = parts[0].strip().lower(), parts[1].strip().lower()

        for date_block in data.get('dates', []):
            for game in date_block.get('games', []):
                home = game['teams']['home']['team']['name'].lower()
                away = game['teams']['away']['team']['name'].lower()
                # Partial match — works even when matchup uses short names
                if any(f in home for f in home_frag.split()) and \
                   any(f in away for f in away_frag.split()):
                    utc_time = datetime.fromisoformat(
                        game['gameDate'].replace('Z', '+00:00')
                    )
                    # Convert UTC → ET (UTC-4 during EDT)
                    et_offset = -4
                    et_time   = utc_time.replace(tzinfo=timezone.utc)
                    et_hour   = (utc_time.hour + et_offset) % 24
                    suffix    = 'PM' if et_hour >= 12 else 'AM'
                    display   = et_hour % 12 or 12
                    return f'{display}:{utc_time.minute:02d} {suffix} ET'
    except Exception:
        pass
    return 'TBD'


# ─────────────────────────────────────────────
# Bet rationale summary
# ─────────────────────────────────────────────

def generate_summary(signal: dict) -> str:
    """
    Write a 1-2 sentence plain-English reason for the bet.
    Built from the signal data — no API call needed.
    """
    player     = signal.get('player', 'This pitcher')
    side       = str(signal.get('side', ''))
    line       = signal.get('line')
    prop_type  = str(signal.get('prop_type', 'pitcher_strikeouts'))
    ev         = float(signal.get('ev', 0))
    edge       = float(signal.get('model_prob', 0)) - float(signal.get('implied_prob', 0))
    k9_curr    = signal.get('k9_current')
    k9_hist    = signal.get('k9_historical')
    k9_trend   = str(signal.get('k9_trend', 'NEW'))
    xfip       = signal.get('xfip')
    ip_per_start = signal.get('ip_per_start')

    parts = []

    if prop_type == 'pitcher_innings':
        avg_ip_str = f'{float(ip_per_start):.1f}' if ip_per_start and not pd.isna(ip_per_start) else None

        if side == 'Over':
            if avg_ip_str:
                parts.append(
                    f'{player} has averaged {avg_ip_str} innings per start, '
                    f'making {line:.1f}+ IP a realistic expectation.'
                )
            else:
                parts.append(f'Model favours {player} going deep into this game.')
        else:
            if avg_ip_str and float(ip_per_start) < float(line):
                parts.append(
                    f'{player} has averaged only {avg_ip_str} IP per start — '
                    f'clearing {line:.1f} innings would be above their norm.'
                )
            else:
                parts.append(
                    f'Early hook risk or tough matchup makes the under {line:.1f} IP attractive here.'
                )

    else:
        # Strikeout prop
        if k9_curr and k9_hist and not pd.isna(k9_hist):
            k9_c = float(k9_curr)
            k9_h = float(k9_hist)
            delta = k9_c - k9_h

            if k9_trend == 'UP':
                parts.append(
                    f'{player} is striking out batters at {k9_c:.1f} K/9 this season, '
                    f'up from a {k9_h:.1f} historical average — stuff is trending sharper.'
                )
            elif k9_trend == 'DOWN':
                parts.append(
                    f'{player} is running at {k9_c:.1f} K/9, below their {k9_h:.1f} career norm — '
                    f'the {side.lower()} aligns with that regression.'
                )
            else:
                parts.append(
                    f'{player} is on pace for {k9_c:.1f} K/9, consistent with their '
                    f'{k9_h:.1f} historical average — a reliable profile.'
                )
        elif k9_curr and not pd.isna(k9_curr):
            parts.append(
                f'{player} is posting {float(k9_curr):.1f} K/9 this season.'
            )

        if xfip and not pd.isna(xfip):
            xfip_f = float(xfip)
            if xfip_f < 3.0:
                parts.append(f'Elite xFIP of {xfip_f:.2f} confirms the underlying dominance.')
            elif xfip_f < 3.8:
                parts.append(f'Solid xFIP of {xfip_f:.2f} backs up the strikeout rate.')
            elif xfip_f > 4.5 and side == 'Under':
                parts.append(f'Elevated xFIP of {xfip_f:.2f} suggests regression — under has extra value.')

    # Always close with the edge
    parts.append(
        f'Model probability is {float(signal.get("model_prob",0)):.0%} vs '
        f'the book\'s {float(signal.get("implied_prob",0)):.0%} — '
        f'a {edge:+.0%} edge at {ev:+.0%} EV.'
    )

    return ' '.join(parts)


# ─────────────────────────────────────────────
# Core send function
# ─────────────────────────────────────────────

def send_alert(signal: dict, webhook_url: str = None) -> bool:
    """
    Send one EV signal as a Discord embed.

    signal dict keys (all from ev_signals.csv):
      player, side, line, odds, ev, model_prob, implied_prob,
      kelly_dollars, matchup, ip_per_start
      Optional: xfip, book

    Returns True if Discord accepted the message (HTTP 204).
    """
    from discord_webhook import DiscordWebhook, DiscordEmbed

    url = webhook_url or DISCORD_WEBHOOK_CONSERVATIVE
    if not url or url == 'your_discord_webhook_url_here':
        print('  ERROR: DISCORD_WEBHOOK_CONSERVATIVE not set in .env')
        return False

    ev           = float(signal['ev'])
    edge         = float(signal['model_prob']) - float(signal['implied_prob'])
    xfip         = signal.get('xfip')
    ip_per_start = signal.get('ip_per_start')

    tier_label, color, tier_emoji = get_tier(ev)
    iq_score = calculate_playbook_iq(ev, edge, xfip, ip_per_start)

    odds_display = f"{int(signal['odds']):+d}"
    game_time    = get_game_time(str(signal.get('matchup', '')))
    matchup_str  = signal.get('matchup', 'Unknown')
    book_str     = signal.get('book', 'Multiple')

    # ── Build embed ──────────────────────────────
    hook  = DiscordWebhook(url=url, rate_limit_retry=True)
    embed = DiscordEmbed(color=color)

    embed.set_author(
        name='PLAYBOOK EDGE ALERT',
        icon_url='https://upload.wikimedia.org/wikipedia/commons/thumb/a/a7/Camponotus_flavomarginatus_ant.jpg/320px-Camponotus_flavomarginatus_ant.jpg'
    )

    prop_type = signal.get('prop_type', 'pitcher_strikeouts')
    prop_label = {
        'pitcher_strikeouts': f'{signal["side"]} {signal["line"]:.1f} Ks',
        'pitcher_innings':    f'{signal["side"]} {signal["line"]:.1f} IP',
    }.get(str(prop_type), f'{signal["side"]} {signal["line"]:.1f}')

    embed.set_title(
        f'{tier_emoji}  {signal["player"]}  —  {prop_label}  {odds_display}'
    )

    embed.set_description(
        f'```\n'
        f'PlaybookIQ   {iq_bar(iq_score)}\n'
        f'```'
    )

    # Row 1
    embed.add_embed_field(name='EV',            value=f'**{ev:+.1%}**',                       inline=True)
    embed.add_embed_field(name='Tier',          value=f'{tier_emoji} {tier_label}',            inline=True)
    embed.add_embed_field(name='Kelly Stake',   value=f'**${signal["kelly_dollars"]:.0f}**',   inline=True)

    # Row 2
    embed.add_embed_field(name='Model Prob',    value=f'{float(signal["model_prob"]):.1%}',    inline=True)
    embed.add_embed_field(name='Implied Prob',  value=f'{float(signal["implied_prob"]):.1%}',  inline=True)
    embed.add_embed_field(name='Edge',          value=f'{edge:+.1%}',                          inline=True)

    # Row 3
    embed.add_embed_field(name='Prop',          value=prop_label, inline=True)
    embed.add_embed_field(name='Book',          value=book_str,                                 inline=True)
    embed.add_embed_field(name='Game Time',     value=game_time,                                inline=True)

    # Row 4 — context fields
    xfip_str = f'{float(xfip):.2f}' if xfip is not None and not pd.isna(xfip) else 'N/A'
    ip_str   = f'{float(ip_per_start):.1f}' if ip_per_start is not None and not pd.isna(ip_per_start) else 'N/A'

    embed.add_embed_field(name='xFIP (season)', value=xfip_str,    inline=True)
    embed.add_embed_field(name='IP/Start',      value=ip_str,      inline=True)
    embed.add_embed_field(name='Matchup',       value=matchup_str, inline=True)

    # Row 5 — plain-English rationale
    summary = generate_summary(signal)
    embed.add_embed_field(name='Why we like it', value=summary, inline=False)

    embed.set_footer(text=f'Playbook Edge  •  {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')
    embed.set_timestamp()

    hook.add_embed(embed)
    response = hook.execute()

    success = hasattr(response, 'status_code') and response.status_code in (200, 204)
    return success


# ─────────────────────────────────────────────
# Fire alerts from ev_signals.csv
# ─────────────────────────────────────────────

def fire_alerts_from_signals(signals_df: pd.DataFrame,
                              ev_threshold: float = 0.04,
                              max_alerts: int = 5,
                              webhook_url: str = None) -> int:
    """
    Send Discord alerts for all rows above ev_threshold.
    Capped at max_alerts to avoid spamming.
    Returns count of successful sends.
    """
    flagged = signals_df[signals_df['ev'] >= ev_threshold].copy()
    flagged = flagged.sort_values('ev', ascending=False)

    if flagged.empty:
        print(f'  No signals above {ev_threshold:.0%} EV — no alerts sent.')
        return 0

    cap     = min(len(flagged), max_alerts)
    sent    = 0
    skipped = 0

    print(f'  Sending {cap} alert(s) (capped at {max_alerts})...')

    for _, row in flagged.head(cap).iterrows():
        ok = send_alert(row.to_dict(), webhook_url)
        if ok:
            sent    += 1
            print(f'    Sent: {row["player"]}  {row["side"]}  {row["line"]:.1f}  EV {row["ev"]:+.1%}')
        else:
            skipped += 1
            print(f'    Failed: {row["player"]}')

    return sent


# ─────────────────────────────────────────────
# Standalone test / demo
# ─────────────────────────────────────────────

def run():
    print('=' * 55)
    print('  PLAYBOOK -- Discord Alert Test')
    print('=' * 55)

    processed = os.path.join(os.path.dirname(__file__), '..', 'data', 'processed')
    signals_path = os.path.join(processed, 'ev_signals.csv')

    if not os.path.exists(signals_path):
        print('No ev_signals.csv found. Run models/ev_calculator.py first.')
        return

    df = pd.read_csv(signals_path)
    flagged = df[df['ev'] >= 0.04].sort_values('ev', ascending=False)

    if flagged.empty:
        print('No signals above 4% EV found.')
        return

    best = flagged.iloc[0]
    print(f'\nBest signal found:')
    print(f'  {best["player"]}  {best["side"]}  {best["line"]:.1f}  '
          f'EV {best["ev"]:+.1%}  Kelly ${best["kelly_dollars"]:.0f}')
    print(f'\nSending to Discord...')

    ok = send_alert(best.to_dict())
    if ok:
        print('  Alert sent successfully! Check your Discord channel.')
    else:
        print('  Send failed. Check DISCORD_WEBHOOK_CONSERVATIVE in .env')


if __name__ == '__main__':
    run()

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
import json
import requests
import pandas as pd
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import DISCORD_WEBHOOK_CONSERVATIVE, DISCORD_WEBHOOK_HEALTH

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

def _rule_based_summary(signal: dict) -> str:
    """Fallback summary when Claude API is unavailable."""
    player       = signal.get('player', 'This pitcher')
    side         = str(signal.get('side', ''))
    line         = signal.get('line')
    prop_type    = str(signal.get('prop_type', 'pitcher_strikeouts'))
    ev           = float(signal.get('ev', 0))
    edge         = float(signal.get('model_prob', 0)) - float(signal.get('implied_prob', 0))
    k9_curr      = signal.get('k9_current')
    k9_hist      = signal.get('k9_historical')
    k9_trend     = str(signal.get('k9_trend', 'NEW'))
    xfip         = signal.get('xfip')
    ip_per_start = signal.get('ip_per_start')
    parts = []

    if prop_type == 'pitcher_innings':
        avg_ip_str = f'{float(ip_per_start):.1f}' if ip_per_start and not pd.isna(ip_per_start) else None
        if side == 'Over':
            if avg_ip_str:
                parts.append(f'{player} has averaged {avg_ip_str} innings per start, making {line:.1f}+ IP a realistic expectation.')
            else:
                parts.append(f'Model favours {player} going deep into this game.')
        else:
            if avg_ip_str and float(ip_per_start) < float(line):
                parts.append(f'{player} has averaged only {avg_ip_str} IP per start — clearing {line:.1f} innings would be above their norm.')
            else:
                parts.append(f'Early hook risk makes the under {line:.1f} IP attractive here.')
    else:
        if k9_curr and k9_hist and not pd.isna(k9_hist):
            k9_c, k9_h = float(k9_curr), float(k9_hist)
            if k9_trend == 'UP':
                parts.append(f'{player} is striking out batters at {k9_c:.1f} K/9 this season, up from a {k9_h:.1f} historical average — stuff is trending sharper.')
            elif k9_trend == 'DOWN':
                parts.append(f'{player} is running at {k9_c:.1f} K/9, below their {k9_h:.1f} career norm — the {side.lower()} aligns with that regression.')
            else:
                parts.append(f'{player} is on pace for {k9_c:.1f} K/9, consistent with their {k9_h:.1f} historical average — a reliable profile.')
        elif k9_curr and not pd.isna(k9_curr):
            parts.append(f'{player} is posting {float(k9_curr):.1f} K/9 this season.')
        if xfip and not pd.isna(xfip):
            xfip_f = float(xfip)
            if xfip_f < 3.0:
                parts.append(f'Elite xFIP of {xfip_f:.2f} confirms the underlying dominance.')
            elif xfip_f < 3.8:
                parts.append(f'Solid xFIP of {xfip_f:.2f} backs up the strikeout rate.')
            elif xfip_f > 4.5 and side == 'Under':
                parts.append(f'Elevated xFIP of {xfip_f:.2f} suggests regression — under has extra value.')

    velo_trend = signal.get('velo_trend')
    if velo_trend is not None and not pd.isna(velo_trend) and prop_type != 'pitcher_innings':
        vt = float(velo_trend)
        if vt >= 0.8:
            parts.append(f'Fastball velocity is up {vt:.1f} mph over the last week — gaining steam.')
        elif vt <= -0.8:
            parts.append(f'Fastball velocity is down {abs(vt):.1f} mph over the last week — a concern.')

    parts.append(
        f'Model probability is {float(signal.get("model_prob", 0)):.0%} vs '
        f'the book\'s {float(signal.get("implied_prob", 0)):.0%} — '
        f'a {edge:+.0%} edge at {ev:+.0%} EV.'
    )
    return ' '.join(parts)


def generate_summary(signal: dict) -> str:
    """
    Generate a short plain-English rationale for the bet using Claude.
    Falls back to rule-based summary if the API is unavailable.
    """
    try:
        import anthropic
        from config import ANTHROPIC_API_KEY
        if not ANTHROPIC_API_KEY:
            return _rule_based_summary(signal)

        player       = signal.get('player', 'Unknown')
        side         = signal.get('side', '')
        line         = signal.get('line', '')
        prop_type    = signal.get('prop_type', 'pitcher_strikeouts')
        ev           = float(signal.get('ev', 0))
        edge         = float(signal.get('model_prob', 0)) - float(signal.get('implied_prob', 0))
        k9_curr      = signal.get('k9_current')
        k9_hist      = signal.get('k9_historical')
        k9_trend     = signal.get('k9_trend', 'NEW')
        xfip         = signal.get('xfip')
        ip_per_start = signal.get('ip_per_start')
        model_prob   = float(signal.get('model_prob', 0))
        implied_prob = float(signal.get('implied_prob', 0))

        prop_desc = (
            f'{side} {line} strikeouts' if prop_type == 'pitcher_strikeouts'
            else f'{side} {line} innings pitched'
        )

        facts = [f'Prop: {player} — {prop_desc}']
        if k9_curr and not pd.isna(k9_curr):
            facts.append(f'Current K/9: {float(k9_curr):.1f}')
        if k9_hist and not pd.isna(k9_hist):
            facts.append(f'Historical K/9 (2yr avg): {float(k9_hist):.1f} — trend: {k9_trend}')
        if xfip and not pd.isna(xfip):
            facts.append(f'xFIP: {float(xfip):.2f}')
        if ip_per_start and not pd.isna(ip_per_start):
            facts.append(f'Avg IP/start: {float(ip_per_start):.1f}')
        velo_trend = signal.get('velo_trend')
        spin_rate  = signal.get('spin_rate')
        pitch_mix  = signal.get('pitch_mix')
        if velo_trend is not None and not pd.isna(velo_trend):
            facts.append(f'Velocity trend (last 7d vs 30d avg): {float(velo_trend):+.1f} mph')
        if spin_rate is not None and not pd.isna(spin_rate):
            facts.append(f'Fastball spin rate: {float(spin_rate):.0f} RPM')
        if pitch_mix is not None and not pd.isna(pitch_mix):
            try:
                mix = json.loads(str(pitch_mix))
                top = sorted(mix.items(), key=lambda x: x[1], reverse=True)[:3]
                facts.append('Pitch mix: ' + ' · '.join(f'{k} {v:.0%}' for k, v in top))
            except Exception:
                pass
        prob_capped   = signal.get('prob_capped')
        low_line_note = signal.get('low_line_note')
        if prob_capped:
            facts.append('Note: model probability was capped at 75% ceiling')
        if low_line_note and not pd.isna(low_line_note):
            facts.append(f'Note: {low_line_note}')
        facts.append(f'Model probability: {model_prob:.0%} | Book implied: {implied_prob:.0%}')
        facts.append(f'Edge: {edge:+.0%} | EV: {ev:+.0%}')

        prompt = (
            'You write short, punchy betting rationales for a baseball prop betting bot. '
            'Given these stats, write exactly 2 sentences explaining why this is a good bet. '
            'Be direct and specific — reference the numbers. No fluff, no disclaimers.\n\n'
            + '\n'.join(facts)
        )

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=120,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return response.content[0].text.strip()

    except Exception:
        return _rule_based_summary(signal)


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

    opp_k_pct      = signal.get('opp_k_pct')
    matchup_factor = signal.get('matchup_factor')
    opp_team       = signal.get('opp_team')

    if opp_k_pct is not None and not pd.isna(opp_k_pct) and matchup_factor is not None:
        factor_f  = float(matchup_factor)
        arrow     = '▲' if factor_f > 1.02 else ('▼' if factor_f < 0.98 else '—')
        opp_k_str = f'{float(opp_k_pct):.1%}  {arrow}  {factor_f:.2f}x'
    else:
        opp_k_str = 'N/A'

    embed.add_embed_field(name='xFIP (season)', value=xfip_str,    inline=True)
    embed.add_embed_field(name='IP/Start',      value=ip_str,      inline=True)
    embed.add_embed_field(name='Matchup',       value=matchup_str, inline=True)

    embed.add_embed_field(name='Opp K-Rate', value=opp_k_str,                               inline=True)
    embed.add_embed_field(name='Opp Team',   value=str(opp_team) if opp_team else 'N/A',   inline=True)

    # Velocity trend
    velo_trend_val = signal.get('velo_trend')
    if velo_trend_val is not None and not pd.isna(velo_trend_val):
        vt = float(velo_trend_val)
        arrow = '↑' if vt > 0.3 else ('↓' if vt < -0.3 else '→')
        velo_trend_str = f'{vt:+.1f} mph {arrow}'
    else:
        velo_trend_str = 'N/A'

    # Fastball spin rate
    spin_val = signal.get('spin_rate')
    spin_str = f'{int(float(spin_val)):,} RPM' if spin_val is not None and not pd.isna(spin_val) else 'N/A'

    # Pitch mix — top 3 pitch types
    pitch_mix_val = signal.get('pitch_mix')
    pitch_mix_str = 'N/A'
    if pitch_mix_val is not None and not pd.isna(pitch_mix_val):
        try:
            mix = json.loads(str(pitch_mix_val))
            top = sorted(mix.items(), key=lambda x: x[1], reverse=True)[:3]
            pitch_mix_str = ' · '.join(f'{k} {v:.0%}' for k, v in top)
        except Exception:
            pass

    embed.add_embed_field(name='Velo Trend',   value=velo_trend_str, inline=True)
    embed.add_embed_field(name='FB Spin Rate', value=spin_str,        inline=True)
    embed.add_embed_field(name='Pitch Mix',    value=pitch_mix_str,   inline=True)
    embed.add_embed_field(name='\u200b',       value='\u200b',        inline=True)  # spacer

    # Plain-English rationale
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
                              webhook_url: str = None) -> int:
    """
    Send Discord alerts for all rows above ev_threshold using tiered caps.

    Caps per tier (signals sorted by PlaybookIQ descending within each tier
    so the highest-quality plays fire first when a tier is over its cap):
      Conservative  4–7%  EV  →  max 5
      Moderate      7–12% EV  →  max 4
      Aggressive   12–20% EV  →  max 3
      Degen         20%+  EV  →  max 1

    Returns total count of successful sends.
    """
    TIER_CAPS = {
        'CONSERVATIVE': 5,
        'MODERATE':     4,
        'AGGRESSIVE':   3,
        'DEGEN':        1,
    }

    def _tier_name(ev: float) -> str:
        if ev >= 0.20: return 'DEGEN'
        if ev >= 0.12: return 'AGGRESSIVE'
        if ev >= 0.07: return 'MODERATE'
        return 'CONSERVATIVE'

    def _iq(row) -> int:
        edge = float(row['model_prob']) - float(row['implied_prob'])
        return calculate_playbook_iq(
            float(row['ev']), edge,
            row.get('xfip'), row.get('ip_per_start')
        )

    flagged = signals_df[signals_df['ev'] >= ev_threshold].copy()

    if flagged.empty:
        print(f'  No signals above {ev_threshold:.0%} EV — no alerts sent.')
        return 0

    flagged['_tier'] = flagged['ev'].apply(_tier_name)
    flagged['_iq']   = flagged.apply(_iq, axis=1)

    sent        = 0
    tier_counts = {}

    for tier, cap in TIER_CAPS.items():
        subset = (
            flagged[flagged['_tier'] == tier]
            .sort_values('_iq', ascending=False)
            .head(cap)
        )
        tier_sent = 0
        for _, row in subset.iterrows():
            ok = send_alert(row.to_dict(), webhook_url)
            if ok:
                sent      += 1
                tier_sent += 1
                print(f'    Sent [{tier}]: {row["player"]}  {row["side"]}  '
                      f'{row["line"]:.1f}  EV {row["ev"]:+.1%}  IQ {row["_iq"]}')
            else:
                print(f'    Failed: {row["player"]}')
        tier_counts[tier] = tier_sent

    summary = '  |  '.join(
        f'{t}: {c}' for t, c in tier_counts.items() if c > 0
    )
    print(f'  Alerts sent: {sent} total  ({summary})')
    return sent


# -----------------------------------------------
# Health / operational alert functions
# These send to DISCORD_WEBHOOK_HEALTH only --
# never to the bet alert channels.
# -----------------------------------------------

def send_pipeline_summary(results: dict,
                          runtime_seconds: int = None,
                          signal_count: int = None,
                          tier_breakdown: dict = None):
    """
    Send a pipeline run summary to the health channel.

    results: {step_name: str} — each value is either a plain success
             string (e.g. 'completed in 12.3s') or an error string
             starting with 'ERROR:' (e.g. 'ERROR: 401 Unauthorized').
    tier_breakdown: optional {tier_name: count} e.g.
             {'CONSERVATIVE': 3, 'MODERATE': 2, 'AGGRESSIVE': 1, 'DEGEN': 0}

    Always prints a terminal preview. Sends to Discord only if
    DISCORD_WEBHOOK_HEALTH is configured.
    """
    from discord_webhook import DiscordWebhook, DiscordEmbed

    passed     = sum(1 for v in results.values() if not str(v).startswith('ERROR:'))
    total      = len(results)
    all_ok     = passed == total
    color      = 0x2ECC71 if all_ok else 0xE74C3C
    top_icon   = '✅' if all_ok else '⚠️'

    runtime_str = (f'{runtime_seconds // 60}m {runtime_seconds % 60}s'
                   if runtime_seconds is not None else 'N/A')
    signals_str = str(signal_count) if signal_count is not None else 'N/A'

    # Build tier breakdown line: e.g. "🟢 3 · 🟡 2 · 🔴 1 · 🎰 0 — 6 total"
    TIER_EMOJI = {'CONSERVATIVE': '🟢', 'MODERATE': '🟡', 'AGGRESSIVE': '🔴', 'DEGEN': '🎰'}
    if tier_breakdown:
        parts = [f'{TIER_EMOJI.get(t, t)} {c}' for t, c in tier_breakdown.items()]
        total_alerts = sum(tier_breakdown.values())
        tier_str = ' · '.join(parts) + f' — {total_alerts} total'
        tier_str_term = '  |  '.join(f'{t}: {c}' for t, c in tier_breakdown.items()) + f'  (total: {total_alerts})'
    else:
        tier_str      = 'N/A'
        tier_str_term = 'N/A'

    title = f'{top_icon}  Pipeline Complete — {passed}/{total} steps passed'

    status_lines = []
    for step, note in results.items():
        icon = '❌' if str(note).startswith('ERROR:') else '✅'
        status_lines.append(f'{icon}  {step}: {note}')

    # Always print a preview to the terminal
    term_lines = [
        ('[OK] ' if not str(v).startswith('ERROR:') else '[FAIL] ') + f'{k}: {v}'
        for k, v in results.items()
    ]
    print('\n' + '-' * 50)
    print('  HEALTH EMBED PREVIEW -- send_pipeline_summary')
    print('-' * 50)
    print(f'  Title : Pipeline Complete -- {passed}/{total} steps passed')
    for line in term_lines:
        print(f'          {line}')
    print(f'  Runtime        : {runtime_str}')
    print(f'  Signals flagged: {signals_str}')
    print(f'  Alerts by tier : {tier_str_term}')
    print(f'  Next run       : Tomorrow 10:30 AM ET')
    print(f'  Timestamp      : {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')
    print('-' * 50 + '\n')

    url = DISCORD_WEBHOOK_HEALTH
    if not url:
        print('  DISCORD_WEBHOOK_HEALTH not set — preview only, not sent.\n')
        return

    hook  = DiscordWebhook(url=url, rate_limit_retry=True)
    embed = DiscordEmbed(title=title, color=color)
    embed.set_description('```\n' + '\n'.join(status_lines) + '\n```')
    embed.add_embed_field(name='Runtime',          value=runtime_str,          inline=True)
    embed.add_embed_field(name='Signals flagged',  value=signals_str,          inline=True)
    embed.add_embed_field(name='Next run',         value='Tomorrow 10:30 AM ET', inline=True)
    embed.add_embed_field(name='Alerts by tier',   value=tier_str,             inline=False)
    embed.set_footer(text=f'Playbook Health  •  {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')
    embed.set_timestamp()
    hook.add_embed(embed)
    hook.execute()


def send_error_alert(step_name: str, error_message: str):
    """
    Send an immediate error alert to the health channel.
    Called as soon as a pipeline step fails — pipeline continues afterward.

    Always prints a terminal preview. Sends to Discord only if
    DISCORD_WEBHOOK_HEALTH is configured.
    """
    from discord_webhook import DiscordWebhook, DiscordEmbed

    title = f'🚨  Pipeline Error — {step_name}'

    print('\n' + '-' * 50)
    print('  HEALTH EMBED PREVIEW -- send_error_alert')
    print('-' * 50)
    print(f'  Title  : [ERROR] Pipeline Error -- {step_name}')
    print(f'  Step   : {step_name}')
    print(f'  Error  : {error_message}')
    print(f'  Status : Pipeline continued')
    print(f'  Time   : {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')
    print('-' * 50 + '\n')

    url = DISCORD_WEBHOOK_HEALTH
    if not url:
        print('  DISCORD_WEBHOOK_HEALTH not set — preview only, not sent.\n')
        return

    hook  = DiscordWebhook(url=url, rate_limit_retry=True)
    embed = DiscordEmbed(title=title, color=0xE74C3C)
    embed.add_embed_field(name='Step',            value=step_name,                  inline=True)
    embed.add_embed_field(name='Status',          value='Pipeline continued',       inline=True)
    embed.add_embed_field(name='Error',
                          value=f'```{error_message[:500]}```',
                          inline=False)
    embed.set_footer(text=f'Playbook Health  •  {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')
    embed.set_timestamp()
    hook.add_embed(embed)
    hook.execute()


def send_heartbeat(game_count: int, pending_trades: int,
                   wins: int = 0, losses: int = 0):
    """
    Send a daily alive-check to the health channel.
    Confirms the bot is running, how many games are on today,
    how many paper trades are still pending, and the season W/L record.

    Always prints a terminal preview. Sends to Discord only if
    DISCORD_WEBHOOK_HEALTH is configured.
    """
    from discord_webhook import DiscordWebhook, DiscordEmbed

    title    = '✅  Playbook is alive'
    resolved = wins + losses
    win_pct  = f'{wins / resolved:.1%}' if resolved > 0 else 'N/A'
    record   = f'{wins}W – {losses}L ({win_pct})'

    print('\n' + '-' * 50)
    print('  HEALTH EMBED PREVIEW -- send_heartbeat')
    print('-' * 50)
    print(f'  Title          : [OK] Playbook is alive')
    print(f'  Record         : {record}')
    print(f'  Games today    : {game_count}')
    print(f'  Pending trades : {pending_trades}')
    print(f'  Next pipeline  : Tomorrow 10:30 AM ET')
    print(f'  Time           : {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')
    print('-' * 50 + '\n')

    url = DISCORD_WEBHOOK_HEALTH
    if not url:
        print('  DISCORD_WEBHOOK_HEALTH not set -- preview only, not sent.\n')
        return

    hook  = DiscordWebhook(url=url, rate_limit_retry=True)
    embed = DiscordEmbed(title=title, color=0x2ECC71)
    embed.add_embed_field(name='Season Record',  value=record,                   inline=True)
    embed.add_embed_field(name='Pending trades', value=str(pending_trades),      inline=True)
    embed.add_embed_field(name='Next pipeline',  value='Tomorrow 10:30 AM ET',   inline=True)
    embed.add_embed_field(name='Games today',    value=str(game_count),          inline=True)
    embed.add_embed_field(name='Wins',           value=str(wins),                inline=True)
    embed.add_embed_field(name='Losses',         value=str(losses),              inline=True)
    embed.set_footer(text=f'Playbook Health  •  {datetime.now().strftime("%b %d, %Y  %I:%M %p")}')
    embed.set_timestamp()
    hook.add_embed(embed)
    hook.execute()


# -----------------------------------------------
# Daily card — all bets as one stacked Discord message
# -----------------------------------------------

def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f'{n}th'
    return f'{n}{["th","st","nd","rd","th"][min(n % 10, 4)]}'


def _velo_trend_label(velo_trend) -> str:
    if velo_trend is None or (isinstance(velo_trend, float) and pd.isna(velo_trend)):
        return 'Velocity data unavailable'
    vt = float(velo_trend)
    if vt >= 0.5:
        return f'Velocity trending up {vt:+.1f} mph over last 7 days'
    if vt <= -0.5:
        return f'Velocity trending down {vt:+.1f} mph over last 7 days'
    return 'Velocity stable over last 7 days'


def _krate_rank_sentence(opp_team: str, throws: str, krates_df: pd.DataFrame) -> str:
    if not opp_team or krates_df is None:
        return ''
    col  = 'vs_RHP' if str(throws).upper() != 'L' else 'vs_LHP'
    hand = 'RHP'   if str(throws).upper() != 'L' else 'LHP'
    df   = krates_df.dropna(subset=[col]).copy()
    if df.empty:
        return ''
    df    = df.sort_values(col, ascending=False).reset_index(drop=True)
    match = df[df['team'] == opp_team]
    if match.empty:
        return ''
    rank  = int(match.index[0]) + 1
    total = len(df)
    kpct  = float(match.iloc[0][col])
    NAMES = {
        'ATH': 'Oakland',      'ATL': 'Atlanta',        'AZ': 'Arizona',
        'BAL': 'Baltimore',    'BOS': 'Boston',          'CHC': 'Chicago Cubs',
        'CWS': 'Chicago White Sox', 'CIN': 'Cincinnati', 'CLE': 'Cleveland',
        'COL': 'Colorado',     'DET': 'Detroit',         'HOU': 'Houston',
        'KC':  'Kansas City',  'LAA': 'LA Angels',       'LAD': 'LA Dodgers',
        'MIA': 'Miami',        'MIL': 'Milwaukee',       'MIN': 'Minnesota',
        'NYM': 'NY Mets',      'NYY': 'NY Yankees',      'PHI': 'Philadelphia',
        'PIT': 'Pittsburgh',   'SD':  'San Diego',        'SEA': 'Seattle',
        'SF':  'San Francisco','STL': 'St. Louis',        'TB':  'Tampa Bay',
        'TEX': 'Texas',        'TOR': 'Toronto',          'WSH': 'Washington',
    }
    name = NAMES.get(opp_team, opp_team)
    return f'{name} ranks {_ordinal(rank)} of {total} in K-rate vs {hand} this season ({kpct:.1%})'


def _daily_card_narrative(signal: dict, krate_sentence: str) -> str:
    """
    2-3 sentence Claude narrative for a casual bettor.
    Falls back to rule-based summary if the API key is missing.
    """
    try:
        import anthropic
        from config import ANTHROPIC_API_KEY
        if not ANTHROPIC_API_KEY:
            return _rule_based_summary(signal)

        player     = signal.get('player', 'Unknown')
        side       = signal.get('side', '')
        line       = signal.get('line', '')
        prop_type  = str(signal.get('prop_type', 'pitcher_strikeouts'))
        k9_curr    = signal.get('k9_current')
        k9_hist    = signal.get('k9_historical')
        k9_trend   = signal.get('k9_trend', 'NEW')
        xfip       = signal.get('xfip')
        velo_trend = signal.get('velo_trend')

        prop_desc = (
            f'{side} {line} strikeouts' if prop_type == 'pitcher_strikeouts'
            else f'{side} {line} innings pitched'
        )

        facts = [f'Pitcher: {player}', f'Prop: {prop_desc}']
        if k9_curr and not pd.isna(k9_curr):
            facts.append(f'K/9 this season: {float(k9_curr):.1f}')
        if k9_hist and not pd.isna(k9_hist):
            facts.append(f'Career avg K/9: {float(k9_hist):.1f} (trend vs history: {k9_trend})')
        if xfip and not pd.isna(xfip):
            facts.append(f'xFIP: {float(xfip):.2f}')
        if velo_trend is not None and not pd.isna(velo_trend):
            facts.append(f'Velocity last 7 days vs 30-day avg: {float(velo_trend):+.1f} mph')
        if krate_sentence:
            facts.append(f'Opponent: {krate_sentence}')

        prompt = (
            'You write short betting write-ups for a casual baseball fan who wants to understand '
            'why a bet is worth considering. Write 2-3 sentences about this prop. '
            'Mention the pitcher by name, reference the strikeout or innings line, '
            'and use at least one stat from the list below. '
            'Be confident and clear — no hype, no disclaimers, no emojis.\n\n'
            + '\n'.join(facts)
        )

        client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=200,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return response.content[0].text.strip()

    except Exception:
        return _rule_based_summary(signal)


def send_daily_card(signals_df: pd.DataFrame,
                    ev_threshold: float = 0.04,
                    max_bets: int = 5,
                    webhook_url: str = None,
                    dry_run: bool = False) -> int:
    """
    Send today's flagged bets as a single Discord message (one embed per bet,
    all stacked in one webhook call).

    Removes: model prob, edge %, EV %, Kelly stake.
    Shows: tier badge, bet title, book/odds/time, IQ bar, velo trend,
           K-rate rank sentence, Claude narrative.

    dry_run=True prints the preview without sending to Discord.
    """
    from discord_webhook import DiscordWebhook, DiscordEmbed

    url = webhook_url or DISCORD_WEBHOOK_CONSERVATIVE
    if not url and not dry_run:
        print('  ERROR: DISCORD_WEBHOOK_CONSERVATIVE not set in .env')
        return 0

    flagged = signals_df[signals_df['ev'] >= ev_threshold].copy()
    flagged = flagged.sort_values('ev', ascending=False).head(max_bets)

    if flagged.empty:
        print(f'  No signals above {ev_threshold:.0%} EV -- no daily card sent.')
        return 0

    # Load team K-rates for ranking
    krates_df = None
    try:
        krates_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', 'data', 'raw', 'team_krates.csv')
        )
        if os.path.exists(krates_path):
            krates_df = pd.read_csv(krates_path)
    except Exception:
        pass

    hook = DiscordWebhook(url=url or 'http://dry-run', rate_limit_retry=True)
    bets = list(flagged.iterrows())

    print(f'\n{"=" * 52}')
    print(f'  DAILY CARD PREVIEW ({len(bets)} bet(s))')
    print(f'{"=" * 52}')

    for idx, (_, row) in enumerate(bets):
        signal  = row.to_dict()
        is_last = (idx == len(bets) - 1)
        ev      = float(signal['ev'])
        edge    = float(signal['model_prob']) - float(signal['implied_prob'])
        xfip    = signal.get('xfip')
        ip_ps   = signal.get('ip_per_start')

        # Tier — swap DEGEN emoji to casino chip per spec
        tier_label, color, tier_emoji = get_tier(ev)
        if tier_label == 'DEGEN':
            tier_emoji = '\U0001f3b0'   # 🎰

        # IQ bar — ▓░ for Discord, ##.. for terminal
        iq_score       = calculate_playbook_iq(ev, edge, xfip, ip_ps)
        filled         = round(iq_score / 10)
        iq_bar_discord = '\u2593' * filled + '\u2591' * (10 - filled)
        iq_bar_term    = '#' * filled + '.' * (10 - filled)

        # Velo trend
        velo_trend = signal.get('velo_trend')
        if velo_trend is None or (isinstance(velo_trend, float) and pd.isna(velo_trend)):
            trend_emoji = '\u26aa'        # ⚪
        elif float(velo_trend) >= 0.5:
            trend_emoji = '\U0001f4c8'   # 📈
        elif float(velo_trend) <= -0.5:
            trend_emoji = '\U0001f4c9'   # 📉
        else:
            trend_emoji = '\u27a1\ufe0f' # ➡️

        trend_label = _velo_trend_label(velo_trend)

        # Bet title and subline
        prop_type  = str(signal.get('prop_type', 'pitcher_strikeouts'))
        side       = signal.get('side', '')
        line       = float(signal.get('line', 0))
        prop_label = (
            f'Over {line:.1f} Ks'  if side == 'Over' and prop_type == 'pitcher_strikeouts' else
            f'Under {line:.1f} Ks' if prop_type == 'pitcher_strikeouts' else
            f'{side} {line:.1f} IP'
        )
        bet_title    = f'{signal["player"]} \u2014 {prop_label}'
        odds_display = f'{int(signal["odds"]):+d}'
        game_time    = get_game_time(str(signal.get('matchup', '')))
        book_str     = signal.get('book', 'Multiple')
        subline      = f'{book_str} \u00b7 {odds_display} \u00b7 {game_time}'

        # K-rate rank sentence
        opp_team       = signal.get('opp_team')
        throws         = str(signal.get('throws', 'R'))
        krate_sentence = _krate_rank_sentence(opp_team, throws, krates_df)

        # Claude narrative
        narrative = _daily_card_narrative(signal, krate_sentence)

        # Discord embed description
        desc_lines = [
            f'**{bet_title}**',
            subline,
            '',
            f'PlaybookIQ: **{iq_score}/100**  {iq_bar_discord}',
            f'{trend_emoji}  {trend_label}',
        ]
        if krate_sentence:
            desc_lines.append(krate_sentence)
        desc_lines += ['', narrative]
        desc = '\n'.join(desc_lines)

        embed = DiscordEmbed(
            title=f'{tier_emoji}  {tier_label}',
            description=desc,
            color=color,
        )
        embed.set_timestamp()
        if is_last:
            embed.set_footer(
                text=(
                    '\u26a0\ufe0f  For entertainment purposes only. Must be 21+. '
                    'Past results do not guarantee future performance.'
                )
            )
        hook.add_embed(embed)

        # Terminal preview (ASCII-safe)
        print(f'\n  Tier    : {tier_label}')
        print(f'  Bet     : {signal["player"]} -- {prop_label}')
        print(f'  Subline : {book_str} | {odds_display} | {game_time}')
        print(f'  IQ      : {iq_score}/100  [{iq_bar_term}]')
        print(f'  Trend   : {trend_label}')
        if krate_sentence:
            print(f'  K-rate  : {krate_sentence}')
        print(f'  Narrative: {narrative}')
        if not is_last:
            print('\n  ' + '- ' * 24)

    print(f'\n  Footer: For entertainment purposes only. Must be 21+.')
    print(f'{"=" * 52}\n')

    if dry_run:
        print('  dry_run=True -- preview only, nothing sent.')
        return len(bets)

    response = hook.execute()
    success  = hasattr(response, 'status_code') and response.status_code in (200, 204)
    if success:
        print(f'  Daily card sent ({len(bets)} bet(s)).')
    else:
        print(f'  Daily card send failed.')
    return len(bets) if success else 0


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
    print('Daily card preview (dry run -- nothing sent to Discord)...\n')

    fake_signals = pd.DataFrame([{
        'player':           'Lance McCullers Jr.',
        'prop_type':        'pitcher_strikeouts',
        'side':             'Over',
        'line':             6.5,
        'odds':             -115,
        'ev':               0.09,
        'model_prob':       0.62,
        'implied_prob':     0.53,
        'flag':             True,
        'book':             'DraftKings',
        'matchup':          'Astros @ Red Sox',
        'k9_current':       10.4,
        'k9_historical':    9.6,
        'k9_trend':         'UP',
        'k9_used':          10.1,
        'xfip':             3.12,
        'ip_per_start':     6.1,
        'hist_reliability': 85,
        'velo_trend':       1.4,
        'spin_rate':        2480,
        'pitch_mix':        '{"FF": 0.42, "SL": 0.31, "CH": 0.18, "CU": 0.09}',
        'throws':           'R',
        'opp_team':         'BOS',
        'opp_k_pct':        0.271,
        'matchup_factor':   1.09,
        'velo_factor':      1.021,
        'kelly_pct':        0.03,
        'kelly_dollars':    30.0,
        'prob_capped':      False,
        'low_line_note':    None,
        'expected_ks':      6.9,
    }])

    send_daily_card(fake_signals, ev_threshold=0.04, dry_run=True)

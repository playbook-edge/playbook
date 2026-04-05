"""
main.py — Entry point for the Playbook betting intelligence bot.

Run this file to start the bot:
    python main.py

Right now it just confirms everything is loaded correctly.
As you build scrapers, models, and alerts, they'll be called from here.
"""

import config


def run():
    print("=== Playbook is starting up ===")

    # Quick config check — warns you if any keys are missing
    missing = []
    if not config.ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not config.SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not config.ODDS_API_KEY:
        missing.append("ODDS_API_KEY")
    if not config.DISCORD_WEBHOOK_URL:
        missing.append("DISCORD_WEBHOOK_URL")

    if missing:
        print(f"Warning: These keys are not set in your .env file: {', '.join(missing)}")
    else:
        print("All API keys loaded.")

    print(f"Sport: {config.SPORT}")
    print(f"Minimum edge to alert: {config.MIN_EDGE_PERCENT}%")
    print(f"Bankroll: ${config.BANKROLL}")
    print("=== Ready. ===")


if __name__ == "__main__":
    run()

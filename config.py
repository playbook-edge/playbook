"""
config.py — Central settings loader for Playbook.

All configuration is read from environment variables (your .env file).
Import this module anywhere you need a setting:
    from config import ANTHROPIC_API_KEY, BANKROLL
"""

import os
from dotenv import load_dotenv

# Load variables from .env file into the environment
load_dotenv()

# --- Claude AI ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# --- Supabase ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# --- Discord ---
DISCORD_WEBHOOK_URL           = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_WEBHOOK_CONSERVATIVE  = os.getenv("DISCORD_WEBHOOK_CONSERVATIVE")
DISCORD_WEBHOOK_PAPER         = os.getenv("DISCORD_WEBHOOK_PAPER")

# --- Odds API ---
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

# --- General Settings ---
SPORT = os.getenv("SPORT", "baseball_mlb")
MIN_EDGE_PERCENT = float(os.getenv("MIN_EDGE_PERCENT", "5"))
BANKROLL = float(os.getenv("BANKROLL", "1000"))

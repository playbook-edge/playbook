# Playbook — Baseball Betting Intelligence Platform

Playbook is an automated baseball betting intelligence system that pulls live odds and stats, uses AI to identify edges, and sends alerts to Discord when a high-value bet is found.

---

## Project Structure

```
playbook/
├── data/
│   ├── raw/          # Raw data exactly as pulled from APIs
│   ├── processed/    # Cleaned and formatted data, ready to use
│   └── historical/   # Past game results and outcomes
├── models/           # Prediction models and edge calculators
├── scrapers/         # Scripts that pull data from APIs and websites
├── alerts/           # Discord and notification logic
├── logs/             # Log files for debugging
├── research/         # Notes, experiments, and analysis notebooks
├── config.py         # Loads all settings from .env
├── main.py           # Run this to start the bot
├── requirements.txt  # Python packages this project needs
├── .env.example      # Template — copy to .env and fill in your keys
└── .gitignore        # Keeps .env and large data files off GitHub
```

---

## Setup

1. Clone this repo
2. Create a virtual environment: `python -m venv venv`
3. Activate it: `venv\Scripts\activate` (Windows) or `source venv/bin/activate` (Mac/Linux)
4. Install dependencies: `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and fill in your API keys
6. Run: `python main.py`

---

## Keys You'll Need

| Key | Where to get it |
|-----|----------------|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `SUPABASE_URL` + `SUPABASE_KEY` | supabase.com → your project settings |
| `DISCORD_WEBHOOK_URL` | Discord channel → Edit → Integrations → Webhooks |
| `ODDS_API_KEY` | the-odds-api.com |

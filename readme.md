# DKB2 NFL Market Monitor

This is a local, paper-only NFL market monitor. It reads the public ESPN schedule and, when ESPN includes them, DraftKings game moneyline, spread, and total prices. It calculates implied and no-vig market probabilities from the paired prices. It does not place real wagers and does not claim an independently validated betting edge.

The free ESPN scoreboard feed currently does not provide the player-prop markets previously shown in the demo. No player props or odds are fabricated when they are unavailable. The DraftKings event-group request in the terminal app is also best-effort and may be blocked or changed by the provider.

On the web monitor's first schedule load for a season, ESPN is queried for both calendar years that can contain that NFL season. The verified regular-season games (weeks 1-18) are saved in the local SQLite database and reused on later loads. Unknown postseason matchups can be added with the round and away/home team inputs; these entries contain no assumed odds or model result. The saved schedule preserves the first-fetched game status and score, while market prices are fetched separately.

The postseason inputs support Wild Card, Divisional, Conference Championship, and Super Bowl placeholders. They store only the round and teams you enter; sportsbook prices and matchup analysis are shown only when their real input data is available.

## Setup

From PowerShell in the project folder:

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run the web monitor

```powershell
python bot_server.py
```

Open <http://127.0.0.1:8000>. The server binds to localhost and serves only the dashboard, not arbitrary project files. No API keys are required. Public data availability and odds coverage can vary.

## Run the terminal monitor

```powershell
python app.py
```

The terminal app prefers DraftKings' public event-group endpoint and falls back to ESPN schedule/game odds when that request is unavailable. A failed live-odds request produces no recommendations.

## Model status

There is currently no independently estimated probability model. No-vig probabilities describe the sportsbook market after removing its quoted overround; they are a market baseline, not a prediction from this tool. The planned model must compare multiple real team and player matchup factors, estimate probability, and account for the offered price and vig. A model edge should only be shown after storing time-stamped picks and outcomes and validating predictions on held-out historical data.

The intended model must compare actual, matchup-relevant team and player factors and account for the offered price and vig before calling a pick positive expected value. That analysis is not implemented or validated yet.
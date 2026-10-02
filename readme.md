# DKB2 NFL Market Monitor

This is a local, paper-only NFL market monitor. It reads the public ESPN schedule and, when ESPN includes them, DraftKings game moneyline, spread, and total prices. It calculates implied and no-vig market probabilities from the paired prices. It does not place real wagers and does not claim an independently validated betting edge.

The free ESPN scoreboard feed currently does not provide the player-prop markets previously shown in the demo. No player props or odds are fabricated when they are unavailable. The DraftKings event-group request in the terminal app is also best-effort and may be blocked or changed by the provider.

On the web monitor's first schedule load for a season, ESPN is queried for both calendar years that can contain that NFL season. The verified regular-season games (weeks 1-18) are saved in the local SQLite database and reused on later loads. Unknown postseason matchups can be added with the round and away/home team inputs; these entries contain no assumed odds or model result. The saved schedule preserves the first-fetched game status and score, while market prices are fetched separately.

The postseason inputs support Wild Card, Divisional, Conference Championship, and Super Bowl placeholders. They store only the round and teams you enter; sportsbook prices and matchup analysis are shown only when their real input data is available.

## Market and result history

The dashboard no longer includes demo picks or fabricated matchups. Markets are sourced from the ESPN feed's DraftKings prices; an outcome pair is retained only when both sides have valid odds. Each successful live-market request stores timestamped prices, source, implied probability, and no-vig market probability in SQLite. A paper wager must reference one of these snapshots, match its captured selection and price, and be recorded within 10 minutes of observation. The UI shows the source and observation time. `GET /api/markets/snapshots` returns recent snapshots.

The observation time is when this app fetched the odds feed, not a provider-confirmed quote update time. The feed does not currently supply a verified per-quote freshness timestamp, so a newly fetched snapshot does not prove a DraftKings price is still available. Treat all displayed odds as informational paper-tracking data, not executable prices. Completed game results are available through `GET /api/history/results`; `GET /api/history/result-changes` and `GET /api/history/settlements` expose correction audits.

Completed ESPN game scores are saved as results, with score changes retained in a correction history. Saved game moneyline, spread, and total paper wagers are automatically settled from their captured selection and final score when the ESPN market/selection can be parsed. Moneyline ties, spread pushes, and total pushes return the stake; ESPN-marked canceled games are voided, while postponed games remain pending. Unknown markets/selections remain pending for manual review. Automatic outcomes can be recalculated if ESPN corrects a final; manual settlements are not overwritten, and every automatic or manual correction is audited. Settlement replaces current ledger PnL rather than adding it twice. These records are paper tracking only and do not generate an independent prediction.

Use **Refresh final results** in the dashboard to re-fetch the selected NFL season from ESPN and update completed scores, including any corrected finals. The API equivalent is `POST /api/history/refresh` with an optional JSON `year` (2000-2100); if ESPN is unavailable, the request fails without changing saved results. Completed games and explicitly canceled games are processed; only completed games with numeric scores are added to the result table. Refreshing collects outcomes and settles linked paper wagers; it does not retrieve historical odds or model features.

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

The stored market snapshots and corrected game results are the initial inputs for that work, not a sufficient historical dataset or backtest by themselves. A future baseline must be trained and evaluated against sourced features using chronological held-out games, with calibration and odds-adjusted performance reported; until then the app intentionally labels every price as a market baseline and generates no model picks.

The refresh process now derives simple team scoring features for scheduled games from up to the team's five most recent completed games in the same season: average points for, against, and point differential. The source observation time for the inputs, feature construction time, target kickoff cutoff, sample count, season, and ESPN source label are retained. Features require an actual ESPN kickoff timestamp, an earlier game kickoff, and proof that DKB2 had already observed that earlier final before the target kickoff; same-time, future, late-observed, and cross-season results are excluded. Re-fetching an unchanged final preserves its first-observed timestamp; a corrected score receives a new observation time. `GET /api/history/team-features` returns the latest saved feature snapshot per team/game, optionally filtered by `event_id`. Rows with zero prior games have null averages, not fabricated defaults.

These features are based only on results DKB2 had actually observed before each target kickoff. A bulk historical refresh performed after games does not retroactively establish that availability, so it will not generate usable pregame inputs for those old targets. The retained rows are not yet a validated historical training set or proof of ESPN's first publication time. ESPN's per-event summary endpoint can expose box-score data for completed games, and the terminal app reads select player stats from it, but there is not yet a consistent timestamped team/player stat dataset. Validate ESPN coverage and schema before adding features or building a prediction baseline; do not infer missing values.
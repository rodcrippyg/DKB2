import http.server
import json
import math
import re
import sqlite3
import socketserver
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from curl_cffi import requests
from app import fetch_espn_fallback

PORT = 8000
DB_FILE = "ledger.db"
ESPN_SUMMARY_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# Short cache for public schedule queries.
SCHEDULE_CACHE = {}
SCHEDULE_CACHE_TTL = 60

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Enable Write-Ahead Logging for high-concurrency non-blocking operations
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        
        # 1. Dynamic Wager Ledger
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wagers (
                id TEXT PRIMARY KEY,
                week INTEGER,
                game_id TEXT,
                matchup TEXT,
                side TEXT,
                market TEXT,
                target TEXT,
                odds REAL,
                edge REAL,
                tier INTEGER DEFAULT 2,
                stake REAL,
                status TEXT,
                actual_stat REAL,
                payout REAL,
                net_pnl REAL,
                strategy_mode TEXT DEFAULT 'TIERED',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wagers_status ON wagers(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wagers_week ON wagers(week);")

        wager_columns = {row[1] for row in cursor.execute("PRAGMA table_info(wagers)")}
        if "tier" not in wager_columns:
            cursor.execute("ALTER TABLE wagers ADD COLUMN tier INTEGER DEFAULT 2")
        if "strategy_mode" not in wager_columns:
            cursor.execute("ALTER TABLE wagers ADD COLUMN strategy_mode TEXT DEFAULT 'TIERED'")
        for column, definition in (
            ("snapshot_id", "INTEGER"),
            ("price_source", "TEXT"),
            ("price_observed_at", "TEXT"),
            ("fair_market_prob", "REAL"),
            ("settlement_source", "TEXT DEFAULT 'MANUAL'"),
        ):
            if column not in wager_columns:
                cursor.execute(f"ALTER TABLE wagers ADD COLUMN {column} {definition}")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                market TEXT NOT NULL,
                side TEXT NOT NULL,
                target TEXT NOT NULL,
                odds REAL NOT NULL,
                implied_prob REAL NOT NULL,
                fair_market_prob REAL NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_market_snapshots_event ON market_snapshots(event_id, observed_at);")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS game_results (
                event_id TEXT PRIMARY KEY,
                away_team TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_score INTEGER NOT NULL,
                home_score INTEGER NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                kickoff_utc TEXT,
                season_year INTEGER,
                week INTEGER
            )
        """)
        result_columns = {row[1] for row in cursor.execute("PRAGMA table_info(game_results)")}
        for column, definition in (
            ("kickoff_utc", "TEXT"),
            ("season_year", "INTEGER"),
            ("week", "INTEGER"),
        ):
            if column not in result_columns:
                cursor.execute(f"ALTER TABLE game_results ADD COLUMN {column} {definition}")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS team_game_features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                team TEXT NOT NULL,
                opponent TEXT NOT NULL,
                side TEXT NOT NULL,
                season_year INTEGER NOT NULL,
                week INTEGER,
                kickoff_utc TEXT NOT NULL,
                prior_games INTEGER NOT NULL,
                avg_points_for REAL,
                avg_points_against REAL,
                avg_point_diff REAL,
                feature_name TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                inputs_observed_through TEXT
            )
        """)
        feature_columns = {row[1] for row in cursor.execute("PRAGMA table_info(team_game_features)")}
        if "inputs_observed_through" not in feature_columns:
            cursor.execute("ALTER TABLE team_game_features ADD COLUMN inputs_observed_through TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_team_game_features_event ON team_game_features(event_id, team, observed_at);")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS game_result_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                away_score INTEGER NOT NULL,
                home_score INTEGER NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wager_settlement_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wager_id TEXT NOT NULL,
                previous_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                payout REAL NOT NULL,
                net_pnl REAL NOT NULL,
                occurred_at TEXT NOT NULL
            )
        """)
        settlement_columns = {row[1] for row in cursor.execute("PRAGMA table_info(wager_settlement_history)")}
        if "source" not in settlement_columns:
            cursor.execute("ALTER TABLE wager_settlement_history ADD COLUMN source TEXT DEFAULT 'MANUAL'")

        # 2. Season Schedule Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nfl_games (
                event_id TEXT PRIMARY KEY,
                season_year INTEGER,
                week INTEGER,
                kickoff_utc TEXT,
                away_team TEXT,
                away_abbr TEXT,
                away_score INTEGER,
                home_team TEXT,
                home_abbr TEXT,
                home_score INTEGER,
                status TEXT,
                division_group TEXT,
                venue TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_games_week ON nfl_games(week);")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schedule_snapshots (
                season_year INTEGER NOT NULL,
                week INTEGER NOT NULL,
                fetched_at REAL NOT NULL,
                games_json TEXT NOT NULL,
                PRIMARY KEY (season_year, week)
            )
        """)

        # 3. Capital Injection & Bankroll Audit
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bankroll_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                deposit REAL,
                active_bankroll REAL,
                note TEXT
            )
        """)
        conn.commit()

init_db()

def load_saved_schedule(season_year: int, week: int | None = None) -> list[dict]:
    with sqlite3.connect(DB_FILE) as conn:
        if week is None:
            row = conn.execute(
                "SELECT games_json FROM schedule_snapshots WHERE season_year = ? AND week = 0",
                (season_year,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT games_json FROM schedule_snapshots WHERE season_year = ? AND week = ?",
                (season_year, week),
            ).fetchone()
        games = json.loads(row[0]) if row else []

        query = """
            SELECT event_id, season_year, week, kickoff_utc, away_team, away_abbr,
                   away_score, home_team, home_abbr, home_score, status, division_group, venue
            FROM nfl_games WHERE season_year = ? AND event_id LIKE 'manual-%'
        """
        params = [season_year]
        if week is not None:
            query += " AND week = ?"
            params.append(week)
        for row in conn.execute(query, params):
            games.append({
                "event_id": row[0], "season_year": row[1], "week": row[2], "start": row[3],
                "away_team": row[4], "away_abbr": row[5], "away_score": row[6],
                "home_team": row[7], "home_abbr": row[8], "home_score": row[9],
                "status": row[10], "division": row[11], "venue": row[12], "wagers": [],
            })
        return games


def save_schedule_snapshot(games: list[dict], season_year: int, requested_week: int | None = None) -> None:
    snapshot_week = requested_week if requested_week is not None else 0
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO schedule_snapshots (season_year, week, fetched_at, games_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(season_year, week) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                games_json = excluded.games_json
        """, (season_year, snapshot_week, time.time(), json.dumps(games)))
        for game in games:
            event_id = str(game.get("event_id", ""))
            game_week = game.get("week") or requested_week
            if not event_id or game_week is None:
                continue
            conn.execute("""
                INSERT INTO nfl_games (
                    event_id, season_year, week, kickoff_utc, away_team, away_abbr,
                    away_score, home_team, home_abbr, home_score, status, division_group, venue
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    season_year = excluded.season_year, week = excluded.week,
                    kickoff_utc = excluded.kickoff_utc, away_team = excluded.away_team,
                    away_abbr = excluded.away_abbr, away_score = excluded.away_score,
                    home_team = excluded.home_team, home_abbr = excluded.home_abbr,
                    home_score = excluded.home_score, status = excluded.status,
                    division_group = excluded.division_group, venue = excluded.venue
            """, (
                event_id, season_year, game_week, game.get("start", ""),
                game.get("away_team", game.get("away", "")), game.get("away_abbr", ""),
                game.get("away_score"), game.get("home_team", game.get("home", "")),
                game.get("home_abbr", ""), game.get("home_score"), game.get("status", "Scheduled"),
                game.get("division", "NFL"), game.get("venue", "Venue unavailable"),
            ))
        conn.commit()


def create_manual_matchup(body: dict) -> dict:
    try:
        season_year = int(body.get("year", time.gmtime().tm_year))
        week = int(body["week"])
        away_team = str(body["away_team"]).strip()
        home_team = str(body["home_team"]).strip()
        away_abbr = str(body.get("away_abbr", "")).strip().upper()
        home_abbr = str(body.get("home_abbr", "")).strip().upper()
        if (not 2000 <= season_year <= 2100 or not 1 <= week <= 22
                or not away_team or not home_team or away_team.casefold() == home_team.casefold()
                or len(away_team) > 80 or len(home_team) > 80
                or len(away_abbr) > 5 or len(home_abbr) > 5):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("Provide different away/home teams, year 2000-2100, and week 1-22") from error

    event_id = f"manual-{uuid.uuid4().hex}"
    stage = {
        19: "Wild Card",
        20: "Divisional",
        21: "Conference Championship",
        22: "Super Bowl",
    }.get(week, "Regular Season")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO nfl_games (
                event_id, season_year, week, kickoff_utc, away_team, away_abbr,
                home_team, home_abbr, status, division_group, venue
            ) VALUES (?, ?, ?, '', ?, ?, ?, ?, 'Manual matchup', ?, '')
        """, (
            event_id, season_year, week, away_team, away_abbr,
            home_team, home_abbr, stage,
        ))
        conn.commit()

    return {
        "event_id": event_id, "season_year": season_year, "week": week,
        "away_team": away_team, "away_abbr": away_abbr,
        "home_team": home_team, "home_abbr": home_abbr,
        "status": "Manual matchup", "division": "Manual entry",
        "venue": "Venue unavailable", "wagers": [],
    }


def persist_market_snapshots(games: list[dict], observed_at: str | None = None) -> None:
    observed_at = observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(DB_FILE) as conn:
        for game in games:
            event_id = str(game.get("event_id", ""))
            by_market = {}
            for wager in game.get("wagers", []):
                by_market.setdefault(wager.get("market"), []).append(wager)

            for market, outcomes in by_market.items():
                if not event_id or not market or len(outcomes) != 2:
                    continue
                if {outcome.get("side") for outcome in outcomes} == {"HOME", "AWAY"}:
                    pass
                elif {outcome.get("target", "").split(" ", 1)[0] for outcome in outcomes} != {"Over", "Under"}:
                    continue
                try:
                    valid = all(
                        math.isfinite(float(outcome["odds"]))
                        and float(outcome["odds"]) != 0
                        and math.isfinite(float(outcome["implied_prob"]))
                        and 0 < float(outcome["implied_prob"]) < 1
                        and math.isfinite(float(outcome["fair_market_prob"]))
                        and 0 < float(outcome["fair_market_prob"]) < 1
                        for outcome in outcomes
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if not valid:
                    continue

                for outcome in outcomes:
                    cursor = conn.execute("""
                        INSERT INTO market_snapshots
                        (event_id, market, side, target, odds, implied_prob, fair_market_prob, source, observed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        event_id, market, outcome["side"], outcome["target"], outcome["odds"],
                        outcome["implied_prob"], outcome["fair_market_prob"],
                        outcome.get("source") or "DraftKings via ESPN", observed_at,
                    ))
                    outcome["snapshot_id"] = cursor.lastrowid
                    outcome["observed_at"] = observed_at
        conn.commit()


def resolve_game_wager_result(
    market: str,
    target: str,
    side: str,
    away_team: str,
    home_team: str,
    away_score: int,
    home_score: int,
) -> str | None:
    market_name = market.casefold()
    if "moneyline" in market_name:
        if side not in ("HOME", "AWAY"):
            return None
        if away_score == home_score:
            return "PUSH"
        selected_home = side == "HOME"
        return "WIN" if (home_score > away_score) == selected_home else "LOSS"

    if "spread" in market_name:
        match = re.fullmatch(r"\s*(.*?)\s+([+-]?\d+(?:\.\d+)?)\s*", target)
        if not match:
            return None
        selection_team = match.group(1).strip().casefold()
        if selection_team == home_team.casefold():
            margin = home_score + float(match.group(2)) - away_score
        elif selection_team == away_team.casefold():
            margin = away_score + float(match.group(2)) - home_score
        else:
            return None
        if math.isclose(margin, 0.0, abs_tol=1e-9):
            return "PUSH"
        return "WIN" if margin > 0 else "LOSS"

    if "total" in market_name:
        match = re.fullmatch(r"\s*(over|under)\s+(\d+(?:\.\d+)?)\s*", target, re.IGNORECASE)
        if not match:
            return None
        total = away_score + home_score
        line = float(match.group(2))
        if math.isclose(total, line, abs_tol=1e-9):
            return "PUSH"
        won = total > line if match.group(1).casefold() == "over" else total < line
        return "WIN" if won else "LOSS"
    return None


def settle_cancelled_game_wagers(
    conn: sqlite3.Connection,
    event_id: str,
    observed_at: str,
) -> int:
    wagers = conn.execute("""
        SELECT id, stake, odds, status, settlement_source
        FROM wagers WHERE game_id = ?
    """, (event_id,)).fetchall()
    settled = 0
    for wager_id, stake, odds, status, source in wagers:
        if status != "PENDING" and source != "ESPN_AUTO":
            continue
        if status == "VOID":
            continue
        payout, net_pnl = compute_settlement_pnl(stake, odds, "VOID")
        conn.execute("""
            UPDATE wagers SET status = 'VOID', payout = ?, net_pnl = ?, settlement_source = 'ESPN_AUTO'
            WHERE id = ?
        """, (payout, net_pnl, wager_id))
        conn.execute("""
            INSERT INTO wager_settlement_history
            (wager_id, previous_status, new_status, payout, net_pnl, occurred_at, source)
            VALUES (?, ?, 'VOID', ?, ?, ?, 'ESPN_AUTO')
        """, (wager_id, status, payout, net_pnl, observed_at))
        settled += 1
    return settled


def settle_completed_game_wagers(
    conn: sqlite3.Connection,
    game: dict,
    observed_at: str,
) -> int:
    event_id = str(game.get("event_id", ""))
    try:
        away_score = int(game["away_score"])
        home_score = int(game["home_score"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0
    away_team = game.get("away", game.get("away_team", ""))
    home_team = game.get("home", game.get("home_team", ""))
    wagers = conn.execute("""
        SELECT id, side, market, target, odds, stake, status, settlement_source
        FROM wagers WHERE game_id = ?
    """, (event_id,)).fetchall()
    settled = 0
    for wager in wagers:
        wager_id, side, market, target, odds, stake, status, source = wager
        if status != "PENDING" and source != "ESPN_AUTO":
            continue
        result = resolve_game_wager_result(
            market, target, side, away_team, home_team, away_score, home_score,
        )
        if result is None:
            continue
        payout, net_pnl = compute_settlement_pnl(stake, odds, result)
        if status == result:
            continue
        conn.execute("""
            UPDATE wagers SET status = ?, payout = ?, net_pnl = ?, settlement_source = 'ESPN_AUTO'
            WHERE id = ?
        """, (result, payout, net_pnl, wager_id))
        conn.execute("""
            INSERT INTO wager_settlement_history
            (wager_id, previous_status, new_status, payout, net_pnl, occurred_at, source)
            VALUES (?, ?, ?, ?, ?, ?, 'ESPN_AUTO')
        """, (wager_id, status, result, payout, net_pnl, observed_at))
        settled += 1
    return settled


def save_final_game_results(games: list[dict], observed_at: str | None = None) -> int:
    observed_at = observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    saved_count = 0
    with sqlite3.connect(DB_FILE) as conn:
        for game in games:
            event_id = str(game.get("event_id", ""))
            if event_id and str(game.get("status", "")).casefold() in ("canceled", "cancelled"):
                settle_cancelled_game_wagers(conn, event_id, observed_at)
            if not game.get("completed"):
                continue
            try:
                away_score = int(game["away_score"])
                home_score = int(game["home_score"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not event_id:
                continue
            previous = conn.execute(
                "SELECT away_score, home_score FROM game_results WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if previous is not None and previous != (away_score, home_score):
                conn.execute("""
                    INSERT INTO game_result_history
                    (event_id, away_score, home_score, source, observed_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (event_id, away_score, home_score, "ESPN", observed_at))
            conn.execute("""
                INSERT INTO game_results
                (event_id, away_team, home_team, away_score, home_score, source, observed_at,
                 kickoff_utc, season_year, week)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    away_team = excluded.away_team, home_team = excluded.home_team,
                    away_score = excluded.away_score, home_score = excluded.home_score,
                    source = excluded.source,
                    observed_at = CASE
                        WHEN game_results.away_score != excluded.away_score
                             OR game_results.home_score != excluded.home_score
                        THEN excluded.observed_at ELSE game_results.observed_at
                    END,
                    kickoff_utc = COALESCE(excluded.kickoff_utc, game_results.kickoff_utc),
                    season_year = COALESCE(excluded.season_year, game_results.season_year),
                    week = COALESCE(excluded.week, game_results.week)
            """, (
                event_id, game.get("away", game.get("away_team", "")),
                game.get("home", game.get("home_team", "")), away_score, home_score,
                "ESPN", observed_at, game.get("kickoff_utc"),
                game.get("season_year"), game.get("week"),
            ))
            settle_completed_game_wagers(conn, game, observed_at)
            saved_count += 1
        conn.commit()
    return saved_count


def _parse_utc_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def save_pregame_team_features(
    games: list[dict],
    observed_at: str | None = None,
    prior_game_limit: int = 5,
) -> int:
    if prior_game_limit < 1:
        raise ValueError("prior_game_limit must be positive")
    observed_at = observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    feature_observed_at = _parse_utc_datetime(observed_at)
    if feature_observed_at is None:
        raise ValueError("observed_at must be a timezone-aware timestamp")
    feature_count = 0
    with sqlite3.connect(DB_FILE) as conn:
        results = conn.execute("""
            SELECT season_year, kickoff_utc, away_team, home_team, away_score, home_score, observed_at
            FROM game_results
            WHERE kickoff_utc IS NOT NULL AND season_year IS NOT NULL
        """).fetchall()
        season_results = {}
        for season_year, kickoff, away, home, away_score, home_score, observed_at_result in results:
            kickoff_time = _parse_utc_datetime(kickoff)
            result_observed_at = _parse_utc_datetime(observed_at_result)
            if kickoff_time is None or result_observed_at is None:
                continue
            season_results.setdefault(season_year, []).append(
                (kickoff_time, result_observed_at, away, home, away_score, home_score)
            )

        for game in games:
            event_id = str(game.get("event_id", ""))
            season_year = game.get("season_year")
            kickoff_raw = game.get("kickoff_utc")
            kickoff = _parse_utc_datetime(kickoff_raw)
            away_team = game.get("away", game.get("away_team", ""))
            home_team = game.get("home", game.get("home_team", ""))
            if (not event_id or season_year is None or kickoff is None
                    or kickoff <= feature_observed_at or not away_team or not home_team):
                continue

            results_for_season = season_results.get(season_year, [])
            for team, opponent, side in (
                (away_team, home_team, "AWAY"),
                (home_team, away_team, "HOME"),
            ):
                prior = []
                for (previous_kickoff, result_observed_at, previous_away, previous_home,
                     away_score, home_score) in results_for_season:
                    if previous_kickoff >= kickoff or result_observed_at >= kickoff:
                        continue
                    if team.casefold() == previous_away.casefold():
                        prior.append((previous_kickoff, result_observed_at, away_score, home_score))
                    elif team.casefold() == previous_home.casefold():
                        prior.append((previous_kickoff, result_observed_at, home_score, away_score))
                prior = sorted(prior, key=lambda row: row[0])[-prior_game_limit:]
                count = len(prior)
                points_for = sum(row[2] for row in prior) / count if count else None
                points_against = sum(row[3] for row in prior) / count if count else None
                point_diff = (
                    sum(row[2] - row[3] for row in prior) / count if count else None
                )
                inputs_observed_through = (
                    max(row[1] for row in prior).isoformat(timespec="seconds") if count else None
                )
                conn.execute("""
                    INSERT INTO team_game_features
                    (event_id, team, opponent, side, season_year, week, kickoff_utc, prior_games,
                     avg_points_for, avg_points_against, avg_point_diff, feature_name, source,
                     observed_at, inputs_observed_through)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_id, team, opponent, side, season_year, game.get("week"),
                    kickoff.astimezone(timezone.utc).isoformat(timespec="seconds"), count,
                    points_for, points_against, point_diff, f"previous_up_to_{prior_game_limit}_same_season_games",
                    "ESPN completed game scores", observed_at, inputs_observed_through,
                ))
                feature_count += 1
        conn.commit()
    return feature_count


def build_prospective_model_dataset() -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        results = conn.execute("""
            SELECT event_id, season_year, kickoff_utc, away_team, home_team,
                   away_score, home_score, observed_at
            FROM game_results
            WHERE kickoff_utc IS NOT NULL AND season_year IS NOT NULL
            ORDER BY kickoff_utc, event_id
        """).fetchall()
        feature_rows = conn.execute("""
            SELECT event_id, team, side, prior_games, avg_points_for, avg_points_against,
                   avg_point_diff, observed_at, inputs_observed_through
            FROM team_game_features
        """).fetchall()
        market_rows = conn.execute("""
            SELECT id, event_id, market, side, target, odds, implied_prob, fair_market_prob,
                   source, observed_at
            FROM market_snapshots
            WHERE market = 'Moneyline'
        """).fetchall()

    features_by_game = {}
    for row in feature_rows:
        features_by_game.setdefault(row["event_id"], []).append(row)
    markets_by_game = {}
    for row in market_rows:
        markets_by_game.setdefault(row["event_id"], []).append(row)

    eligible_rows = []
    counts = {
        "finals_with_kickoff": 0,
        "finals_with_observed_pregame_features": 0,
        "finals_with_pregame_moneyline_pair": 0,
        "usable_non_tie_rows": 0,
    }
    by_season = {}

    for result in results:
        kickoff = _parse_utc_datetime(result["kickoff_utc"])
        result_observed_at = _parse_utc_datetime(result["observed_at"])
        if kickoff is None or result_observed_at is None or result_observed_at <= kickoff:
            continue
        counts["finals_with_kickoff"] += 1
        season = by_season.setdefault(str(result["season_year"]), {
            "finals_with_kickoff": 0,
            "finals_with_observed_pregame_features": 0,
            "finals_with_pregame_moneyline_pair": 0,
            "usable_non_tie_rows": 0,
        })
        season["finals_with_kickoff"] += 1

        features = {}
        for feature in features_by_game.get(result["event_id"], []):
            feature_observed_at = _parse_utc_datetime(feature["observed_at"])
            inputs_observed_through = _parse_utc_datetime(feature["inputs_observed_through"])
            if (feature_observed_at is None or feature_observed_at >= kickoff
                    or feature["prior_games"] < 1 or inputs_observed_through is None
                    or inputs_observed_through >= kickoff):
                continue
            existing = features.get(feature["side"])
            if existing is None or feature["observed_at"] > existing["observed_at"]:
                features[feature["side"]] = feature
        if (set(features) != {"HOME", "AWAY"}
                or features["HOME"]["team"].casefold() != result["home_team"].casefold()
                or features["AWAY"]["team"].casefold() != result["away_team"].casefold()):
            continue
        counts["finals_with_observed_pregame_features"] += 1
        season["finals_with_observed_pregame_features"] += 1

        market_pairs = {}
        for market in markets_by_game.get(result["event_id"], []):
            observed_at = _parse_utc_datetime(market["observed_at"])
            if observed_at is None or observed_at >= kickoff:
                continue
            market_pairs.setdefault(market["observed_at"], []).append(market)
        complete_pairs = []
        for observed_at, pair in market_pairs.items():
            by_side = {}
            for item in pair:
                if item["side"] not in ("HOME", "AWAY"):
                    continue
                if item["side"] not in by_side or item["id"] > by_side[item["side"]]["id"]:
                    by_side[item["side"]] = item
            if set(by_side) != {"HOME", "AWAY"}:
                continue
            if (by_side["HOME"]["target"].casefold() != result["home_team"].casefold()
                    or by_side["AWAY"]["target"].casefold() != result["away_team"].casefold()):
                continue
            if any(not math.isfinite(item["fair_market_prob"])
                   or not 0 < item["fair_market_prob"] < 1 for item in pair):
                continue
            if not math.isclose(
                by_side["HOME"]["fair_market_prob"] + by_side["AWAY"]["fair_market_prob"],
                1.0, rel_tol=0.0, abs_tol=0.01,
            ):
                continue
            complete_pairs.append((observed_at, by_side))
        if not complete_pairs:
            continue
        _, moneyline = max(complete_pairs, key=lambda pair: pair[0])
        counts["finals_with_pregame_moneyline_pair"] += 1
        season["finals_with_pregame_moneyline_pair"] += 1
        if result["home_score"] == result["away_score"]:
            continue

        counts["usable_non_tie_rows"] += 1
        season["usable_non_tie_rows"] += 1
        home_feature, away_feature = features["HOME"], features["AWAY"]
        eligible_rows.append({
            "event_id": result["event_id"],
            "season_year": result["season_year"],
            "kickoff_utc": kickoff.isoformat(timespec="seconds"),
            "away_team": result["away_team"],
            "home_team": result["home_team"],
            "away_score": result["away_score"],
            "home_score": result["home_score"],
            "home_win": int(result["home_score"] > result["away_score"]),
            "feature_observed_at": max(
                home_feature["observed_at"], away_feature["observed_at"],
            ),
            "feature_inputs_observed_through": max(
                home_feature["inputs_observed_through"],
                away_feature["inputs_observed_through"],
            ),
            "away_prior_games": away_feature["prior_games"],
            "away_avg_points_for": away_feature["avg_points_for"],
            "away_avg_points_against": away_feature["avg_points_against"],
            "away_avg_point_diff": away_feature["avg_point_diff"],
            "home_prior_games": home_feature["prior_games"],
            "home_avg_points_for": home_feature["avg_points_for"],
            "home_avg_points_against": home_feature["avg_points_against"],
            "home_avg_point_diff": home_feature["avg_point_diff"],
            "moneyline_observed_at": max(item["observed_at"] for item in moneyline.values()),
            "away_odds": moneyline["AWAY"]["odds"],
            "home_odds": moneyline["HOME"]["odds"],
            "away_no_vig_probability": moneyline["AWAY"]["fair_market_prob"],
            "home_no_vig_probability": moneyline["HOME"]["fair_market_prob"],
        })

    return {
        "model_status": "NOT_VALIDATED",
        "model_predictions_enabled": False,
        "rows_are_prospective_and_time_eligible": True,
        "counts": counts,
        "by_season": by_season,
        "eligible_rows": sorted(eligible_rows, key=lambda row: (row["kickoff_utc"], row["event_id"])),
    }


def get_real_player_stat(event_id: str, player_name: str, market_type: str) -> float:
    """Queries official ESPN box-score summary endpoint to settle props against real stats."""
    url = f"{ESPN_SUMMARY_BASE}?event={event_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=6)
        if r.status_code != 200:
            return None
        data = r.json()
        box = data.get("boxscore", {}).get("players", [])
        for team_box in box:
            for stat_cat in team_box.get("statistics", []):
                cat_name = stat_cat.get("name", "").lower()
                for ath in stat_cat.get("athletes", []):
                    name = ath.get("athlete", {}).get("displayName", "")
                    if player_name.lower() in name.lower():
                        stats = ath.get("stats", [])
                        labels = stat_cat.get("labels", [])
                        if "pass" in market_type.lower() and "passing" in cat_name:
                            if "YDS" in labels:
                                idx = labels.index("YDS")
                                return float(stats[idx])
                        elif "rush" in market_type.lower() and "rushing" in cat_name:
                            if "YDS" in labels:
                                idx = labels.index("YDS")
                                return float(stats[idx])
                        elif "rec" in market_type.lower() and "receiving" in cat_name:
                            if "REC" in labels:
                                idx = labels.index("REC")
                                return float(stats[idx])
                            elif "YDS" in labels and "yds" in market_type.lower():
                                idx = labels.index("YDS")
                                return float(stats[idx])
                        elif "td" in market_type.lower():
                            if "TD" in labels:
                                idx = labels.index("TD")
                                return float(stats[idx])
    except Exception as e:
        print(f"[!] Box score lookup failed for {player_name}: {e}")
    return None

def compute_settlement_pnl(stake: float, odds: float, result: bool | str) -> tuple[float, float]:
    if result in ("PUSH", "VOID"):
        return round(stake, 2), 0.0
    won = result is True or result == "WIN"
    if not won:
        return 0.0, -round(stake, 2)
    profit = stake * (odds / 100.0) if odds > 0 else stake * (100.0 / abs(odds))
    return round(stake + profit, 2), round(profit, 2)

def calculate_actual_ledger_stats(strategy_mode="TIERED"):
    """Calculates true PnL and win rate strictly from settled records in SQLite."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status, stake, net_pnl, odds 
            FROM wagers 
            WHERE status IN ('WIN', 'LOSS', 'PUSH', 'VOID')
        """)
        rows = cursor.fetchall()
        
        wins = sum(1 for r in rows if r[0] == "WIN")
        losses = sum(1 for r in rows if r[0] == "LOSS")
        pushes = sum(1 for r in rows if r[0] == "PUSH")
        voids = sum(1 for r in rows if r[0] == "VOID")
        total_bets = wins + losses
        total_staked = sum(r[1] for r in rows if r[0] != "VOID")
        total_net_pnl = sum(r[2] for r in rows)
        win_rate = (wins / total_bets * 100.0) if total_bets > 0 else 0.0
        roi = (total_net_pnl / total_staked * 100.0) if total_staked > 0 else 0.0

        # Query deposits and pending exposure
        cursor.execute("SELECT COALESCE(SUM(deposit), 0.0) FROM bankroll_audit")
        base_deposits = 1000.0 + cursor.fetchone()[0]
        cursor.execute("SELECT SUM(stake) FROM wagers WHERE status = 'PENDING'")
        pending_exposure = cursor.fetchone()[0] or 0.0
        
        current_bankroll = base_deposits + total_net_pnl - pending_exposure

        return {
            "record": f"{wins} – {losses} – {pushes} pushes – {voids} voids",
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "voids": voids,
            "win_rate": f"{win_rate:.1f}%",
            "total_staked": round(total_staked, 2),
            "net_pnl": round(total_net_pnl, 2),
            "roi": f"{roi:+.1f}%",
            "current_bankroll": round(current_bankroll, 2),
            "base_bankroll": round(base_deposits, 2),
            "pending_exposure": round(pending_exposure, 2),
            "sizing_guide": "Paper tracking only"
        }

class UpgradedRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        if self.path.endswith(".html") or self.path == "/":
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("Request body size is invalid")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")
            return body
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self.send_error(400, str(error))
            return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # 1. Performance Ledger API (Sub-millisecond)
        if parsed.path == "/api/ledger/performance":
            query_params = urllib.parse.parse_qs(parsed.query)
            mode = query_params.get("mode", ["TIERED"])[0]
            stats = calculate_actual_ledger_stats(strategy_mode=mode)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(stats).encode("utf-8"))
            return

        # 2. Dynamic Schedule API (Cached & Ingested from SQLite)
        elif parsed.path == "/api/schedule":
            query_params = urllib.parse.parse_qs(parsed.query)
            try:
                week = int(query_params["week"][0]) if "week" in query_params else None
                year = int(query_params.get("year", [str(time.gmtime().tm_year)])[0])
                if week is not None and not 1 <= week <= 22:
                    raise ValueError
                if not 2000 <= year <= 2100:
                    raise ValueError
            except ValueError:
                self.send_error(400, "year must be 2000-2100 and week must be 1-22")
                return

            games = load_saved_schedule(year, week)
            if not games:
                games = fetch_espn_fallback(week=week, year=year, full_season=week is None)
                if games:
                    save_schedule_snapshot(games, year, week)
                    games = load_saved_schedule(year, week)

            if not games:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "Schedule unavailable and no saved schedule exists for this season/week."
                }).encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(games).encode("utf-8"))
            return

        elif parsed.path == "/api/markets":
            games = fetch_espn_fallback()
            if not games:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "Live ESPN schedule and sportsbook markets are unavailable; no recommendations were generated."
                }).encode("utf-8"))
                return

            persist_market_snapshots(games)
            save_final_game_results(games)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([
                {
                    "event_id": game["event_id"], "away": game["away"], "home": game["home"],
                    "away_score": game.get("away_score"), "home_score": game.get("home_score"),
                    "status": game.get("status"), "completed": game.get("completed", False),
                    "wagers": game["wagers"],
                }
                for game in games
            ]).encode("utf-8"))
            return

        elif parsed.path == "/api/markets/snapshots":
            with sqlite3.connect(DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                items = [dict(row) for row in conn.execute("""
                    SELECT id, event_id, market, side, target, odds, implied_prob,
                           fair_market_prob, source, observed_at
                    FROM market_snapshots ORDER BY observed_at DESC, id DESC LIMIT 500
                """)]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(items).encode("utf-8"))
            return

        elif parsed.path == "/api/history/results":
            with sqlite3.connect(DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                items = [dict(row) for row in conn.execute("""
                    SELECT event_id, away_team, home_team, away_score, home_score, source,
                           observed_at, kickoff_utc, season_year, week
                    FROM game_results ORDER BY observed_at DESC LIMIT 500
                """)]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(items).encode("utf-8"))
            return

        elif parsed.path == "/api/history/team-features":
            query_params = urllib.parse.parse_qs(parsed.query)
            event_id = query_params.get("event_id", [None])[0]
            with sqlite3.connect(DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                query = """
                    SELECT f.id, f.event_id, f.team, f.opponent, f.side, f.season_year, f.week,
                           f.kickoff_utc, f.prior_games, f.avg_points_for, f.avg_points_against,
                           f.avg_point_diff, f.feature_name, f.source, f.observed_at,
                           f.inputs_observed_through
                    FROM team_game_features f
                    JOIN (
                        SELECT event_id, team, MAX(id) AS latest_id
                        FROM team_game_features
                        GROUP BY event_id, team
                    ) latest ON latest.latest_id = f.id
                """
                params = ()
                if event_id:
                    query += " WHERE f.event_id = ?"
                    params = (event_id,)
                query += " ORDER BY f.kickoff_utc, f.event_id, f.side"
                items = [dict(row) for row in conn.execute(query, params)]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(items).encode("utf-8"))
            return

        elif parsed.path == "/api/model/dataset":
            dataset = build_prospective_model_dataset()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(dataset).encode("utf-8"))
            return

        elif parsed.path == "/api/history/result-changes":
            with sqlite3.connect(DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                items = [dict(row) for row in conn.execute("""
                    SELECT id, event_id, away_score, home_score, source, observed_at
                    FROM game_result_history ORDER BY observed_at DESC, id DESC LIMIT 500
                """)]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(items).encode("utf-8"))
            return

        elif parsed.path == "/api/history/settlements":
            with sqlite3.connect(DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                items = [dict(row) for row in conn.execute("""
                    SELECT id, wager_id, previous_status, new_status, payout, net_pnl, occurred_at, source
                    FROM wager_settlement_history ORDER BY occurred_at DESC, id DESC LIMIT 500
                """)]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(items).encode("utf-8"))
            return

        # 3. Retrieve All Stored Wagers
        elif parsed.path == "/api/ledger/wagers":
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                c.execute("""
                    SELECT id, week, game_id, matchup, side, market, target, odds, edge, tier, stake,
                           status, net_pnl, snapshot_id, price_source, price_observed_at, fair_market_prob
                    FROM wagers 
                    ORDER BY timestamp DESC
                """)
                items = [
                    {
                        "id": r[0], "week": r[1], "game_id": r[2], "matchup": r[3], "side": r[4],
                        "market": r[5], "target": r[6], "odds": r[7], "edge": r[8], "tier": r[9],
                        "stake": r[10], "status": r[11], "net_pnl": r[12], "snapshot_id": r[13],
                        "price_source": r[14], "price_observed_at": r[15], "fair_market_prob": r[16],
                    }
                    for r in c.fetchall()
                ]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(items).encode("utf-8"))
            return

        if parsed.path in ("/", "/index.html"):
            return super().do_GET()
        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/schedule/manual":
            body = self._read_json_body()
            if body is None:
                return
            try:
                game = create_manual_matchup(body)
            except ValueError as error:
                self.send_error(400, str(error))
                return

            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "game": game}).encode("utf-8"))
            return

        if parsed.path == "/api/history/refresh":
            body = self._read_json_body()
            if body is None:
                return
            try:
                default_year = datetime.now().year - (1 if datetime.now().month < 3 else 0)
                year = int(body.get("year", default_year))
                if not 2000 <= year <= 2100:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                self.send_error(400, "year must be 2000-2100")
                return

            games = fetch_espn_fallback(year=year, full_season=True)
            if not games:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "ESPN season results are unavailable; saved results were not changed."
                }).encode("utf-8"))
                return

            saved_count = save_final_game_results(games)
            feature_count = save_pregame_team_features(games)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "season_year": year,
                "games_fetched": len(games),
                "completed_results_saved": saved_count,
                "team_feature_rows_saved": feature_count,
            }).encode("utf-8"))
            return

        # 1. Place Paper Bet Endpoint
        if parsed.path == "/api/bets/place":
            body = self._read_json_body()
            if body is None:
                return

            try:
                wager_id = str(body["id"]).strip()
                odds = float(body.get("oddsNum", -110))
                edge = float(body.get("edgeVal", 0))
                tier = int(body.get("tier", 2))
                stake = float(body.get("stake", 25))
                week = int(body.get("week", 4))
                snapshot_id = int(body["snapshot_id"])
                if (not wager_id or len(wager_id) > 128 or not math.isfinite(odds) or odds == 0
                        or not math.isfinite(edge) or not math.isfinite(stake) or stake <= 0
                        or tier not in (1, 2) or week < 1 or snapshot_id <= 0):
                    raise ValueError
            except (KeyError, TypeError, ValueError, OverflowError):
                self.send_error(400, "Invalid wager fields")
                return

            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                snapshot = cursor.execute("""
                    SELECT event_id, market, target, odds, source, observed_at, fair_market_prob, side
                    FROM market_snapshots WHERE id = ?
                """, (snapshot_id,)).fetchone()
                if not snapshot:
                    self.send_error(400, "A saved real-market snapshot is required")
                    return
                try:
                    observed_at = datetime.fromisoformat(snapshot[5])
                    if observed_at.tzinfo is None:
                        observed_at = observed_at.replace(tzinfo=timezone.utc)
                    age_seconds = (datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()
                except (TypeError, ValueError):
                    self.send_error(400, "Market snapshot timestamp is invalid")
                    return
                if age_seconds < 0 or age_seconds > 600:
                    self.send_error(409, "Market price is stale; refresh live markets before recording a paper wager")
                    return
                snapshot_side = "SHARED" if snapshot[7] == "BOTH" else snapshot[7]
                if (str(body.get("game_id", "")) != snapshot[0]
                        or str(body.get("market", "")) != snapshot[1]
                        or str(body.get("target", "")) != snapshot[2]
                        or odds != snapshot[3]
                        or body.get("side") != snapshot_side):
                    self.send_error(400, "Wager details do not match the saved market snapshot")
                    return
                cursor.execute("""
                    INSERT OR IGNORE INTO wagers
                    (id, week, game_id, matchup, side, market, target, odds, edge, tier, stake, status,
                     payout, net_pnl, strategy_mode, snapshot_id, price_source, price_observed_at, fair_market_prob)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0.0, 0.0, ?, ?, ?, ?, ?)
                """, (
                    wager_id, week, body.get("game_id", ""), body.get("matchup", ""),
                    snapshot_side, snapshot[1], snapshot[2],
                    odds, edge, tier, stake, body.get("strategy_mode", "TIERED"),
                    snapshot_id, snapshot[4], snapshot[5], snapshot[6],
                ))
                if cursor.rowcount != 1:
                    self.send_error(409, "A wager with this ID already exists")
                    return
                conn.commit()

            stats = calculate_actual_ledger_stats(strategy_mode=body.get("strategy_mode", "TIERED"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "stats": stats}).encode("utf-8"))
            return

        # 2. Settle Single Wager Endpoint
        elif parsed.path == "/api/bets/settle":
            body = self._read_json_body()
            if body is None:
                return
            
            wager_id = body.get("id")
            result = body.get("result")
            if result is None and isinstance(body.get("won"), bool):
                result = "WIN" if body["won"] else "LOSS"
            if (not isinstance(wager_id, str) or not wager_id
                    or result not in ("WIN", "LOSS", "PUSH", "VOID")):
                self.send_error(400, "A wager ID and WIN, LOSS, PUSH, or VOID result are required")
                return

            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT stake, odds, status FROM wagers WHERE id = ?", (wager_id,))
                row = cursor.fetchone()
                if not row:
                    self.send_error(404, "Wager does not exist")
                    return
                stake, odds, previous_status = row
                payout, net_pnl = compute_settlement_pnl(stake, odds, result)
                cursor.execute("""
                    UPDATE wagers
                    SET status = ?, payout = ?, net_pnl = ?, settlement_source = 'MANUAL'
                    WHERE id = ?
                """, (result, payout, net_pnl, wager_id))
                cursor.execute("""
                    INSERT INTO wager_settlement_history
                    (wager_id, previous_status, new_status, payout, net_pnl, occurred_at, source)
                    VALUES (?, ?, ?, ?, ?, ?, 'MANUAL')
                """, (
                    wager_id, previous_status, result, payout, net_pnl,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ))
                conn.commit()

            stats = calculate_actual_ledger_stats()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "stats": stats}).encode("utf-8"))
            return

        # 3. Add Weekly Bankroll Deposit Endpoint
        elif parsed.path == "/api/bankroll/deposit":
            body = self._read_json_body()
            if body is None:
                return
            try:
                deposit_amount = float(body.get("amount", 100.0))
                if not math.isfinite(deposit_amount) or deposit_amount <= 0:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                self.send_error(400, "Deposit amount must be a positive finite number")
                return
            note = body.get("note", "Weekly Booster")

            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO bankroll_audit (deposit, active_bankroll, note)
                    VALUES (?, 1000.0 + (SELECT COALESCE(SUM(deposit), 0.0) FROM bankroll_audit) + ?, ?)
                """, (deposit_amount, deposit_amount, note))
                conn.commit()

            stats = calculate_actual_ledger_stats()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "stats": stats}).encode("utf-8"))
            return

        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    # ThreadingHTTPServer handles simultaneous network requests instantly without queuing
    with ThreadingHTTPServer(("127.0.0.1", PORT), UpgradedRequestHandler) as httpd:
        print(f"[⚡] Ultra-Fast High-Concurrency DKB2 Server running at http://localhost:{PORT}")
        httpd.serve_forever()
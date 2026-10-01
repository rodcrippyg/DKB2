import http.server
import json
import math
import sqlite3
import socketserver
import time
import urllib.parse
import uuid
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

def compute_settlement_pnl(stake: float, odds: float, won: bool) -> tuple[float, float]:
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
            WHERE status IN ('WIN', 'LOSS')
        """)
        rows = cursor.fetchall()
        
        wins = sum(1 for r in rows if r[0] == "WIN")
        losses = sum(1 for r in rows if r[0] == "LOSS")
        total_bets = wins + losses
        total_staked = sum(r[1] for r in rows)
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
            "record": f"{wins} – {losses}",
            "wins": wins,
            "losses": losses,
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

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([
                {"event_id": game["event_id"], "away": game["away"], "home": game["home"], "wagers": game["wagers"]}
                for game in games
            ]).encode("utf-8"))
            return

        # 3. Retrieve All Stored Wagers
        elif parsed.path == "/api/ledger/wagers":
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                c.execute("""
                    SELECT id, week, game_id, matchup, side, market, target, odds, edge, tier, stake, status, net_pnl
                    FROM wagers 
                    ORDER BY timestamp DESC
                """)
                items = [
                    {
                        "id": r[0], "week": r[1], "game_id": r[2], "matchup": r[3], "side": r[4],
                        "market": r[5], "target": r[6], "odds": r[7], "edge": r[8], "tier": r[9],
                        "stake": r[10], "status": r[11], "net_pnl": r[12]
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
                if (not wager_id or len(wager_id) > 128 or not math.isfinite(odds) or odds == 0
                        or not math.isfinite(edge) or not math.isfinite(stake) or stake <= 0
                        or tier not in (1, 2) or week < 1):
                    raise ValueError
            except (KeyError, TypeError, ValueError, OverflowError):
                self.send_error(400, "Invalid wager fields")
                return

            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR IGNORE INTO wagers
                    (id, week, game_id, matchup, side, market, target, odds, edge, tier, stake, status, payout, net_pnl, strategy_mode)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0.0, 0.0, ?)
                """, (
                    wager_id, week, body.get("game_id", ""), body.get("matchup", ""),
                    body.get("side", ""), body.get("market", ""), body.get("target", ""),
                    odds, edge, tier, stake,
                    body.get("strategy_mode", "TIERED")
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
            won = body.get("won")
            if not isinstance(wager_id, str) or not wager_id or not isinstance(won, bool):
                self.send_error(400, "A wager ID and boolean won value are required")
                return

            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT stake, odds FROM wagers WHERE id = ? AND status = 'PENDING'", (wager_id,))
                row = cursor.fetchone()
                if not row:
                    self.send_error(409, "Wager does not exist or is already settled")
                    return
                stake, odds = row[0], row[1]
                payout, net_pnl = compute_settlement_pnl(stake, odds, won)
                cursor.execute("""
                    UPDATE wagers
                    SET status = ?, payout = ?, net_pnl = ?
                    WHERE id = ? AND status = 'PENDING'
                """, ('WIN' if won else 'LOSS', payout, net_pnl, wager_id))
                if cursor.rowcount != 1:
                    self.send_error(409, "Wager was settled by another request")
                    return
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
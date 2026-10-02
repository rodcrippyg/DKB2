import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import bot_server


class SchedulePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.original_db_file = bot_server.DB_FILE
        bot_server.DB_FILE = os.path.join(self.temp_dir.name, "test-ledger.db")
        bot_server.init_db()

    def tearDown(self):
        bot_server.DB_FILE = self.original_db_file
        self.temp_dir.cleanup()

    def test_schedule_snapshot_persists_for_the_season(self):
        games = [{
            "event_id": "event-1",
            "season_year": 2026,
            "week": 4,
            "away_team": "Away Team",
            "home_team": "Home Team",
            "wagers": [],
        }]
        bot_server.save_schedule_snapshot(games, 2026)

        self.assertEqual(bot_server.load_saved_schedule(2026), games)

    def test_super_bowl_matchup_can_be_saved_and_loaded(self):
        created = bot_server.create_manual_matchup({
            "year": 2026,
            "week": 22,
            "away_team": "Away Team",
            "home_team": "Home Team",
        })

        self.assertEqual(created["division"], "Manual entry")
        self.assertEqual(created["week"], 22)
        saved_games = bot_server.load_saved_schedule(2026, 22)

        self.assertEqual(len(saved_games), 1)
        self.assertEqual(saved_games[0]["away_team"], "Away Team")
        self.assertEqual(saved_games[0]["home_team"], "Home Team")

    def test_manual_matchup_rejects_same_team_on_both_sides(self):
        with self.assertRaises(ValueError):
            bot_server.create_manual_matchup({
                "year": 2026,
                "week": 22,
                "away_team": "Same Team",
                "home_team": " same team ",
            })

    def test_only_complete_valid_market_pairs_are_snapshotted(self):
        observed_at = datetime.now(timezone.utc).isoformat()
        games = [{
            "event_id": "event-1",
            "wagers": [
                {"side": "HOME", "market": "Moneyline", "target": "Home Team", "odds": -110,
                 "implied_prob": 0.5238, "fair_market_prob": 0.5, "source": "DraftKings"},
                {"side": "AWAY", "market": "Moneyline", "target": "Away Team", "odds": -110,
                 "implied_prob": 0.5238, "fair_market_prob": 0.5, "source": "DraftKings"},
                {"side": "BOTH", "market": "Total", "target": "Over 41.5", "odds": -110,
                 "implied_prob": 0.5238, "fair_market_prob": 0.5, "source": "DraftKings"},
            ],
        }]

        bot_server.persist_market_snapshots(games, observed_at)

        with sqlite3.connect(bot_server.DB_FILE) as conn:
            rows = conn.execute(
                "SELECT event_id, market, source, observed_at FROM market_snapshots ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row[1] for row in rows}, {"Moneyline"})
        self.assertTrue(all(row[2:] == ("DraftKings", observed_at) for row in rows))
        self.assertEqual(len([w for w in games[0]["wagers"] if "snapshot_id" in w]), 2)

    def test_final_game_results_are_correctable(self):
        game = {
            "event_id": "event-1", "away": "Away Team", "home": "Home Team",
            "away_score": "20", "home_score": "17", "completed": True,
            "season_year": 2026, "week": 2, "kickoff_utc": "2026-09-01T17:00:00Z",
        }
        bot_server.save_final_game_results([game], "2026-10-01T20:00:00+00:00")
        bot_server.save_final_game_results([game], "2026-10-01T21:00:00+00:00")
        with sqlite3.connect(bot_server.DB_FILE) as conn:
            first_observed_at = conn.execute(
                "SELECT observed_at FROM game_results WHERE event_id = 'event-1'"
            ).fetchone()[0]
        self.assertEqual(first_observed_at, "2026-10-01T20:00:00+00:00")
        game["home_score"] = "21"
        bot_server.save_final_game_results([game], "2026-10-02T20:00:00+00:00")
        game["completed"] = False
        game["home_score"] = "30"
        bot_server.save_final_game_results([game], "2026-10-03T20:00:00+00:00")

        with sqlite3.connect(bot_server.DB_FILE) as conn:
            result = conn.execute(
                "SELECT away_score, home_score, observed_at, kickoff_utc, season_year, week "
                "FROM game_results WHERE event_id = 'event-1'"
            ).fetchone()
            history_count = conn.execute(
                "SELECT COUNT(*) FROM game_result_history WHERE event_id = 'event-1'"
            ).fetchone()[0]
        self.assertEqual(result, (
            20, 21, "2026-10-02T20:00:00+00:00", "2026-09-01T17:00:00Z", 2026, 2,
        ))
        self.assertEqual(history_count, 1)

    def test_espn_player_stat_parser_preserves_raw_and_numeric_values(self):
        payload = {
            "boxscore": {
                "players": [{
                    "team": {"id": "12", "displayName": "Seattle Seahawks"},
                    "statistics": [{
                        "name": "passing",
                        "keys": ["completions/passingAttempts", "passingYards", "interceptions"],
                        "labels": ["C/ATT", "YDS", "INT"],
                        "athletes": [{
                            "athlete": {"id": "99", "displayName": "Example Player"},
                            "stats": ["18/27", "245", "1"],
                        }],
                    }],
                }],
            },
        }

        stats = bot_server.parse_espn_player_game_stats(payload)

        self.assertEqual(len(stats), 3)
        self.assertEqual(stats[0]["player_key"], "99")
        self.assertEqual(stats[0]["team_name"], "Seattle Seahawks")
        self.assertEqual(stats[0]["category"], "passing")
        self.assertEqual(stats[0]["stat_key"], "completions/passingAttempts")
        self.assertEqual(stats[0]["raw_value"], "18/27")
        self.assertIsNone(stats[0]["numeric_value"])
        self.assertEqual(stats[1]["numeric_value"], 245.0)
        self.assertEqual(stats[2]["numeric_value"], 1.0)
        self.assertIsNone(bot_server.parse_espn_player_game_stats({}))

    def test_player_game_stats_are_idempotent_and_keep_correction_history(self):
        stat = {
            "player_key": "99", "player_id": "99", "player_name": "Example Player",
            "team_id": "12", "team_name": "Seattle Seahawks", "category": "passing",
            "stat_key": "passingYards", "stat_label": "YDS", "raw_value": "245",
            "numeric_value": 245.0,
        }
        bot_server.save_player_game_stats("event-player", [stat], "2026-10-01T20:00:00+00:00")
        bot_server.save_player_game_stats("event-player", [stat], "2026-10-01T21:00:00+00:00")
        corrected = {**stat, "raw_value": "246", "numeric_value": 246.0}
        bot_server.save_player_game_stats("event-player", [corrected], "2026-10-02T20:00:00+00:00")

        with sqlite3.connect(bot_server.DB_FILE) as conn:
            saved = conn.execute("""
                SELECT raw_value, numeric_value, observed_at
                FROM player_game_stats WHERE event_id = 'event-player'
            """).fetchone()
            history = conn.execute("""
                SELECT previous_raw_value, new_raw_value, observed_at
                FROM player_game_stat_history WHERE event_id = 'event-player'
            """).fetchall()
            sync = conn.execute("""
                SELECT status, rows_saved FROM player_stats_sync WHERE event_id = 'event-player'
            """).fetchone()
        self.assertEqual(saved, ("246", 246.0, "2026-10-02T20:00:00+00:00"))
        self.assertEqual(history, [("245", "246", "2026-10-02T20:00:00+00:00")])
        self.assertEqual(sync, ("SUCCESS", 1))

    def test_player_stats_refresh_is_bounded_retries_failures_and_skips_successes(self):
        games = [
            {
                "event_id": "player-event-1", "season_year": 2026, "week": 1,
                "kickoff_utc": "2026-09-01T17:00:00Z", "away": "Away", "home": "Home",
                "away_score": 20, "home_score": 17, "completed": True,
            },
            {
                "event_id": "player-event-2", "season_year": 2026, "week": 2,
                "kickoff_utc": "2026-09-08T17:00:00Z", "away": "Away", "home": "Home",
                "away_score": 24, "home_score": 21, "completed": True,
            },
        ]
        bot_server.save_final_game_results(games)
        stat = {
            "player_key": "99", "player_name": "Example Player", "category": "passing",
            "stat_key": "passingYards", "raw_value": "245", "numeric_value": 245.0,
        }
        with patch.object(bot_server, "fetch_espn_player_game_stats", return_value=[stat]) as fetch_stats:
            first_batch = bot_server.refresh_player_game_stats(2026, 1)
        self.assertEqual(first_batch, {
            "events_attempted": 1, "events_succeeded": 1, "events_failed": 0,
            "stat_rows_saved": 1, "remaining_events": 1,
        })
        fetch_stats.assert_called_once_with("player-event-1")

        with patch.object(bot_server, "fetch_espn_player_game_stats", return_value=None) as fetch_stats:
            failed_batch = bot_server.refresh_player_game_stats(2026, 10)
        self.assertEqual(failed_batch["events_attempted"], 1)
        self.assertEqual(failed_batch["events_failed"], 1)
        self.assertEqual(failed_batch["remaining_events"], 1)
        fetch_stats.assert_called_once_with("player-event-2")
        with self.assertRaises(ValueError):
            bot_server.refresh_player_game_stats(2026, 26)

    def test_pregame_team_features_use_only_prior_completed_games(self):
        prior_games = [
            {
                "event_id": "prior-1", "season_year": 2026, "week": 1,
                "kickoff_utc": "2026-09-01T17:00:00Z", "away": "Team A", "home": "Team X",
                "away_score": 30, "home_score": 10, "completed": True,
            },
            {
                "event_id": "prior-2", "season_year": 2026, "week": 2,
                "kickoff_utc": "2026-09-08T17:00:00Z", "away": "Team Y", "home": "team a",
                "away_score": 14, "home_score": 21, "completed": True,
            },
            {
                "event_id": "same-time", "season_year": 2026, "week": 3,
                "kickoff_utc": "2026-09-15T17:00:00Z", "away": "Team A", "home": "Team Z",
                "away_score": 99, "home_score": 0, "completed": True,
            },
            {
                "event_id": "future", "season_year": 2026, "week": 5,
                "kickoff_utc": "2026-09-22T17:00:00Z", "away": "Team A", "home": "Team W",
                "away_score": 0, "home_score": 99, "completed": True,
            },
            {
                "event_id": "late-result", "season_year": 2026, "week": 3,
                "kickoff_utc": "2026-09-14T17:00:00Z", "away": "Team A", "home": "Team L",
                "away_score": 99, "home_score": 0, "completed": True,
            },
            {
                "event_id": "other-season", "season_year": 2025, "week": 18,
                "kickoff_utc": "2025-12-20T17:00:00Z", "away": "Team A", "home": "Team Q",
                "away_score": 1, "home_score": 99, "completed": True,
            },
        ]
        observed_at_by_event = {
            "prior-1": "2026-09-02T17:00:00+00:00",
            "prior-2": "2026-09-09T17:00:00+00:00",
            "same-time": "2026-09-15T17:01:00+00:00",
            "future": "2026-09-23T17:00:00+00:00",
            "late-result": "2026-09-16T17:00:00+00:00",
            "other-season": "2025-12-21T17:00:00+00:00",
        }
        for prior_game in prior_games:
            bot_server.save_final_game_results(
                [prior_game], observed_at_by_event[prior_game["event_id"]],
            )
        target = {
            "event_id": "target", "season_year": 2026, "week": 4,
            "kickoff_utc": "2026-09-15T17:00:00Z", "away": "Team A", "home": "Team B",
        }

        saved_count = bot_server.save_pregame_team_features(
            [target], "2026-09-15T16:00:00+00:00",
        )

        self.assertEqual(saved_count, 2)
        with sqlite3.connect(bot_server.DB_FILE) as conn:
            away_features = conn.execute("""
                SELECT prior_games, avg_points_for, avg_points_against, avg_point_diff,
                       kickoff_utc, source, observed_at, inputs_observed_through
                FROM team_game_features WHERE event_id = 'target' AND side = 'AWAY'
            """).fetchone()
            home_features = conn.execute("""
                SELECT prior_games, avg_points_for, avg_points_against
                FROM team_game_features WHERE event_id = 'target' AND side = 'HOME'
            """).fetchone()
        self.assertEqual(away_features[:4], (2, 25.5, 12.0, 13.5))
        self.assertEqual(away_features[4:], (
            "2026-09-15T17:00:00+00:00", "ESPN completed game scores",
            "2026-09-15T16:00:00+00:00", "2026-09-09T17:00:00+00:00",
        ))
        self.assertEqual(home_features, (0, None, None))
        self.assertEqual(
            bot_server.save_pregame_team_features([target], "2026-09-15T18:00:00+00:00"),
            0,
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), bot_server.UpgradedRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/history/team-features?event_id=target")
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(len(payload), 2)
            self.assertEqual({row["team"] for row in payload}, {"Team A", "Team B"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_model_dataset_requires_features_market_and_outcome_before_and_after_kickoff(self):
        events = [
            ("eligible", "2026-10-10T17:00:00+00:00", "2026-10-11T01:00:00+00:00", 24, 20,
             "2026-10-09T17:00:00+00:00", "2026-10-08T17:00:00+00:00"),
            ("late-feature", "2026-10-10T17:00:00+00:00", "2026-10-11T01:00:00+00:00", 24, 20,
             "2026-10-10T18:00:00+00:00", "2026-10-08T17:00:00+00:00"),
            ("late-market", "2026-10-10T17:00:00+00:00", "2026-10-11T01:00:00+00:00", 24, 20,
             "2026-10-09T17:00:00+00:00", "2026-10-10T18:00:00+00:00"),
            ("tie", "2026-10-10T17:00:00+00:00", "2026-10-11T01:00:00+00:00", 20, 20,
             "2026-10-09T17:00:00+00:00", "2026-10-08T17:00:00+00:00"),
            ("early-result", "2026-10-10T17:00:00+00:00", "2026-10-10T16:00:00+00:00", 24, 20,
             "2026-10-09T17:00:00+00:00", "2026-10-08T17:00:00+00:00"),
        ]
        with sqlite3.connect(bot_server.DB_FILE) as conn:
            for event_id, kickoff, result_observed, away_score, home_score, feature_time, market_time in events:
                conn.execute("""
                    INSERT INTO game_results
                    (event_id, season_year, kickoff_utc, away_team, home_team, away_score, home_score,
                     source, observed_at)
                    VALUES (?, 2026, ?, 'Away Team', 'Home Team', ?, ?, 'ESPN', ?)
                """, (event_id, kickoff, away_score, home_score, result_observed))
                for side, team in (("AWAY", "Away Team"), ("HOME", "Home Team")):
                    conn.execute("""
                        INSERT INTO team_game_features
                        (event_id, team, opponent, side, season_year, kickoff_utc, prior_games,
                         avg_points_for, avg_points_against, avg_point_diff, feature_name,
                         source, observed_at, inputs_observed_through)
                        VALUES (?, ?, ?, ?, 2026, ?, 2, 20, 17, 3, 'test', 'ESPN', ?, ?)
                    """, (
                        event_id, team, "Home Team" if side == "AWAY" else "Away Team",
                        side, kickoff, feature_time, "2026-10-08T16:00:00+00:00",
                    ))
                for side, target, odds, fair_probability in (
                    ("AWAY", "Away Team", -110, 0.5),
                    ("HOME", "Home Team", -110, 0.5),
                ):
                    conn.execute("""
                        INSERT INTO market_snapshots
                        (event_id, market, side, target, odds, implied_prob, fair_market_prob,
                         source, observed_at)
                        VALUES (?, 'Moneyline', ?, ?, ?, 0.5238, ?, 'DraftKings', ?)
                    """, (event_id, side, target, odds, fair_probability, market_time))
                if event_id == "eligible":
                    for side, target, odds, fair_probability in (
                        ("AWAY", "Away Team", -115, 0.51),
                        ("HOME", "Home Team", 105, 0.49),
                    ):
                        conn.execute("""
                            INSERT INTO market_snapshots
                            (event_id, market, side, target, odds, implied_prob, fair_market_prob,
                             source, observed_at)
                            VALUES (?, 'Moneyline', ?, ?, ?, 0.5238, ?, 'DraftKings', ?)
                        """, (event_id, side, target, odds, fair_probability, market_time))

        dataset = bot_server.build_prospective_model_dataset()

        self.assertEqual(dataset["model_status"], "NOT_VALIDATED")
        self.assertFalse(dataset["model_predictions_enabled"])
        self.assertEqual(dataset["counts"]["finals_with_kickoff"], 4)
        self.assertEqual(dataset["counts"]["finals_with_observed_pregame_features"], 3)
        self.assertEqual(dataset["counts"]["finals_with_pregame_moneyline_pair"], 2)
        self.assertEqual(dataset["counts"]["usable_non_tie_rows"], 1)
        self.assertEqual([row["event_id"] for row in dataset["eligible_rows"]], ["eligible"])
        self.assertEqual(dataset["eligible_rows"][0]["home_win"], 0)
        self.assertEqual(dataset["eligible_rows"][0]["away_odds"], -115)
        self.assertEqual(dataset["eligible_rows"][0]["home_odds"], 105)

        server = ThreadingHTTPServer(("127.0.0.1", 0), bot_server.UpgradedRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/model/dataset")
            response = connection.getresponse()
            api_dataset = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(api_dataset["counts"], dataset["counts"])
            self.assertEqual(api_dataset["eligible_rows"][0]["event_id"], "eligible")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_push_and_void_return_stake(self):
        self.assertEqual(bot_server.compute_settlement_pnl(20, -110, "PUSH"), (20, 0.0))
        self.assertEqual(bot_server.compute_settlement_pnl(20, 105, "VOID"), (20, 0.0))

    def test_void_wagers_are_excluded_from_settled_roi_exposure(self):
        with sqlite3.connect(bot_server.DB_FILE) as conn:
            conn.executemany("""
                INSERT INTO wagers (id, stake, status, net_pnl, odds)
                VALUES (?, 10, ?, ?, -110)
            """, [
                ("roi-win", "WIN", 5),
                ("roi-loss", "LOSS", -10),
                ("roi-push", "PUSH", 0),
                ("roi-void", "VOID", 0),
            ])
        stats = bot_server.calculate_actual_ledger_stats()
        self.assertEqual(stats["total_staked"], 30)
        self.assertEqual(stats["net_pnl"], -5)
        self.assertEqual(stats["roi"], "-16.7%")

    def test_resolves_moneyline_spread_and_total_results(self):
        cases = [
            ("Moneyline", "Away Team", "AWAY", 20, 17, "WIN"),
            ("Moneyline", "Home Team", "HOME", 20, 17, "LOSS"),
            ("Moneyline", "Away Team", "AWAY", 17, 17, "PUSH"),
            ("Spread", "Home Team +3", "HOME", 20, 17, "PUSH"),
            ("Spread", "Home Team -3", "HOME", 20, 17, "LOSS"),
            ("Spread", "Away Team +3.5", "AWAY", 20, 17, "WIN"),
            ("Spread", "Home Team +3.5", "HOME", 20, 17, "WIN"),
            ("Total", "Over 37", "BOTH", 20, 17, "PUSH"),
            ("Total", "Under 37.5", "BOTH", 20, 17, "WIN"),
            ("Total", "Over 38", "BOTH", 20, 17, "LOSS"),
        ]
        for market, target, side, away_score, home_score, expected in cases:
            with self.subTest(market=market, target=target):
                self.assertEqual(
                    bot_server.resolve_game_wager_result(
                        market, target, side, "Away Team", "Home Team", away_score, home_score,
                    ),
                    expected,
                )
        self.assertIsNone(bot_server.resolve_game_wager_result(
            "Moneyline", "Away Team", "SHARED", "Away Team", "Home Team", 20, 17,
        ))

    def test_completed_results_auto_settle_captured_game_markets_and_recalculate_corrections(self):
        wagers = [
            ("money-away", "AWAY", "Moneyline", "Away Team", -110, "PENDING", "MANUAL"),
            ("spread-home", "HOME", "Spread", "Home Team +3", -110, "PENDING", "MANUAL"),
            ("total-over", "BOTH", "Total", "Over 36.5", -110, "PENDING", "MANUAL"),
            ("unknown-market", "HOME", "Player Props", "Player over 20.5", -110, "PENDING", "MANUAL"),
            ("manual-result", "AWAY", "Moneyline", "Away Team", -110, "LOSS", "MANUAL"),
        ]
        with sqlite3.connect(bot_server.DB_FILE) as conn:
            conn.executemany("""
                INSERT INTO wagers (id, game_id, side, market, target, odds, stake, status, net_pnl, settlement_source)
                VALUES (?, 'event-1', ?, ?, ?, ?, 10, ?, 0, ?)
            """, wagers)

        final = {
            "event_id": "event-1", "away": "Away Team", "home": "Home Team",
            "away_score": 20, "home_score": 17, "completed": True,
        }
        self.assertEqual(bot_server.save_final_game_results([final], "2026-10-01T20:00:00+00:00"), 1)
        with sqlite3.connect(bot_server.DB_FILE) as conn:
            first = dict(conn.execute("SELECT id, status FROM wagers").fetchall())
            first["unknown-market"] = conn.execute(
                "SELECT status FROM wagers WHERE id = 'unknown-market'"
            ).fetchone()[0]
            first["manual-result"] = conn.execute(
                "SELECT status FROM wagers WHERE id = 'manual-result'"
            ).fetchone()[0]
        self.assertEqual(first["money-away"], "WIN")
        self.assertEqual(first["spread-home"], "PUSH")
        self.assertEqual(first["total-over"], "WIN")
        self.assertEqual(first["unknown-market"], "PENDING")
        self.assertEqual(first["manual-result"], "LOSS")

        final["away_score"] = 16
        self.assertEqual(bot_server.save_final_game_results([final], "2026-10-02T20:00:00+00:00"), 1)
        with sqlite3.connect(bot_server.DB_FILE) as conn:
            current = dict(conn.execute("SELECT id, status FROM wagers").fetchall())
            current["total_over_pnl"] = conn.execute(
                "SELECT net_pnl FROM wagers WHERE id = 'total-over'"
            ).fetchone()[0]
            auto_history = conn.execute(
                "SELECT previous_status, new_status, source FROM wager_settlement_history "
                "WHERE wager_id = 'money-away' ORDER BY id"
            ).fetchall()
        self.assertEqual(current["money-away"], "LOSS")
        self.assertEqual(current["spread-home"], "WIN")
        self.assertEqual(current["total-over"], "LOSS")
        self.assertEqual(current["total_over_pnl"], -10)
        self.assertEqual(auto_history, [("PENDING", "WIN", "ESPN_AUTO"), ("WIN", "LOSS", "ESPN_AUTO")])

    def test_cancelled_game_voids_open_wagers_but_postponed_game_does_not(self):
        with sqlite3.connect(bot_server.DB_FILE) as conn:
            conn.executemany("""
                INSERT INTO wagers (id, game_id, market, target, odds, stake, status)
                VALUES (?, ?, 'Moneyline', 'Away Team', -110, 10, 'PENDING')
            """, [("cancelled-wager", "cancelled-game"), ("postponed-wager", "postponed-game")])
        bot_server.save_final_game_results([
            {"event_id": "cancelled-game", "status": "Canceled", "completed": False},
            {"event_id": "postponed-game", "status": "Postponed", "completed": False},
        ], "2026-10-01T20:00:00+00:00")
        with sqlite3.connect(bot_server.DB_FILE) as conn:
            rows = dict(conn.execute("SELECT id, status FROM wagers").fetchall())
            void_data = conn.execute(
                "SELECT payout, net_pnl, settlement_source FROM wagers WHERE id = 'cancelled-wager'"
            ).fetchone()
        self.assertEqual(rows["cancelled-wager"], "VOID")
        self.assertEqual(rows["postponed-wager"], "PENDING")
        self.assertEqual(void_data, (10, 0, "ESPN_AUTO"))

    def test_paper_wager_uses_captured_price_and_corrections_replace_pnl(self):
        game = {
            "event_id": "event-1",
            "wagers": [
                {"side": "AWAY", "market": "Moneyline", "target": "Away Team", "odds": -110,
                 "implied_prob": 0.5238, "fair_market_prob": 0.5, "source": "DraftKings"},
                {"side": "HOME", "market": "Moneyline", "target": "Home Team", "odds": -110,
                 "implied_prob": 0.5238, "fair_market_prob": 0.5, "source": "DraftKings"},
            ],
        }
        bot_server.persist_market_snapshots([game])
        snapshot_id = game["wagers"][0]["snapshot_id"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot_server.UpgradedRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path, body):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = response.status, response.read()
            connection.close()
            return result

        try:
            wager = {
                "id": "paper-1", "week": 4, "game_id": "event-1", "matchup": "Away @ Home",
                "side": "AWAY", "market": "Moneyline", "target": "Away Team",
                "oddsNum": -110, "snapshot_id": snapshot_id, "stake": 12.34,
            }
            status, _ = post("/api/bets/place", wager)
            self.assertEqual(status, 200)
            status, response = post("/api/bets/settle", {"id": "paper-1", "result": "PUSH"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(response)["stats"]["current_bankroll"], 1000.0)
            status, response = post("/api/bets/settle", {"id": "paper-1", "result": "WIN"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(response)["stats"]["current_bankroll"], 1011.22)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        with sqlite3.connect(bot_server.DB_FILE) as conn:
            wager_row = conn.execute(
                "SELECT status, snapshot_id, price_source, price_observed_at, fair_market_prob, net_pnl "
                "FROM wagers WHERE id = 'paper-1'"
            ).fetchone()
            settlements = conn.execute(
                "SELECT previous_status, new_status FROM wager_settlement_history "
                "WHERE wager_id = 'paper-1' ORDER BY id"
            ).fetchall()
        self.assertEqual(wager_row[:3], ("WIN", snapshot_id, "DraftKings"))
        self.assertIsNotNone(wager_row[3])
        self.assertEqual(wager_row[4], 0.5)
        self.assertEqual(wager_row[5], 11.22)
        self.assertEqual(settlements, [("PENDING", "PUSH"), ("PUSH", "WIN")])

    def test_paper_wager_rejects_stale_snapshot(self):
        game = {
            "event_id": "event-1",
            "wagers": [
                {"side": "AWAY", "market": "Moneyline", "target": "Away Team", "odds": -110,
                 "implied_prob": 0.5238, "fair_market_prob": 0.5},
                {"side": "HOME", "market": "Moneyline", "target": "Home Team", "odds": -110,
                 "implied_prob": 0.5238, "fair_market_prob": 0.5},
            ],
        }
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        bot_server.persist_market_snapshots([game], old_time)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot_server.UpgradedRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("POST", "/api/bets/place", json.dumps({
                "id": "stale-paper", "week": 4, "game_id": "event-1", "side": "AWAY",
                "market": "Moneyline", "target": "Away Team", "oddsNum": -110,
                "snapshot_id": game["wagers"][0]["snapshot_id"], "stake": 10,
            }), {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @patch("bot_server.fetch_espn_fallback")
    def test_results_refresh_fetches_and_corrects_completed_games(self, mock_fetch):
        games = [
            {
                "event_id": "final-1", "away": "Away", "home": "Home",
                "away_score": "20", "home_score": "17", "completed": True,
                "season_year": 2026, "week": 4, "kickoff_utc": "2026-10-01T20:00:00Z",
            },
            {
                "event_id": "scheduled-1", "away": "Next Away", "home": "Next Home",
                "away_score": None, "home_score": None, "completed": False,
                "season_year": 2026, "week": 5, "kickoff_utc": "2026-10-08T20:00:00Z",
            },
        ]
        mock_fetch.return_value = games
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot_server.UpgradedRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(body):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request(
                "POST", "/api/history/refresh", json.dumps(body),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            status, payload = response.status, response.read()
            connection.close()
            return status, json.loads(payload) if payload else {}

        try:
            status, payload = post({"year": 2026})
            self.assertEqual(status, 200)
            self.assertEqual(payload["games_fetched"], 2)
            self.assertEqual(payload["completed_results_saved"], 1)
            self.assertEqual(payload["team_feature_rows_saved"], 2)
            mock_fetch.assert_called_once_with(year=2026, full_season=True)

            games[0]["home_score"] = "21"
            status, payload = post({"year": 2026})
            self.assertEqual(status, 200)
            self.assertEqual(payload["completed_results_saved"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        with sqlite3.connect(bot_server.DB_FILE) as conn:
            results = conn.execute(
                "SELECT away_score, home_score FROM game_results WHERE event_id = 'final-1'"
            ).fetchone()
            changed_scores = conn.execute(
                "SELECT away_score, home_score FROM game_result_history WHERE event_id = 'final-1'"
            ).fetchall()
            scheduled_count = conn.execute(
                "SELECT COUNT(*) FROM game_results WHERE event_id = 'scheduled-1'"
            ).fetchone()[0]
        self.assertEqual(results, (20, 21))
        self.assertEqual(changed_scores, [(20, 21)])
        self.assertEqual(scheduled_count, 0)

    @patch("bot_server.fetch_espn_fallback")
    def test_results_refresh_rejects_invalid_year_and_reports_feed_failure(self, mock_fetch):
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot_server.UpgradedRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request(
                "POST", "/api/history/refresh", json.dumps({"year": 1999}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.close()
            mock_fetch.assert_not_called()

            mock_fetch.return_value = []
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request(
                "POST", "/api/history/refresh", json.dumps({"year": 2026}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertIn(b"saved results were not changed", response.read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
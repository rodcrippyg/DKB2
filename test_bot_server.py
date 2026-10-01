import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
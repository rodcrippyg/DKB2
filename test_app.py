import unittest
from unittest.mock import Mock, patch

import app


class MarketProbabilityTests(unittest.TestCase):
    def test_devigs_a_paired_market(self):
        probabilities = app.devig_market([-110, -110])

        self.assertAlmostEqual(probabilities[0], 0.5)
        self.assertAlmostEqual(probabilities[1], 0.5)

    def test_rejects_zero_odds(self):
        with self.assertRaises(ValueError):
            app.american_to_implied(0)

    @patch("app.requests.get")
    def test_incomplete_market_is_not_returned(self, mock_get):
        response = Mock()
        response.json.return_value = {
            "week": {"number": 4},
            "events": [{
                "id": "event-1",
                "competitions": [{
                    "competitors": [
                        {"homeAway": "away", "team": {"displayName": "Away Team", "abbreviation": "AWY"}},
                        {"homeAway": "home", "team": {"displayName": "Home Team", "abbreviation": "HME"}},
                    ],
                    "odds": [{
                        "provider": {"name": "DraftKings"},
                        "moneyline": {
                            "home": {"close": {"odds": "-110"}},
                            "away": {},
                        },
                        "total": {
                            "over": {"close": {"line": "o41.5", "odds": "-110"}},
                        },
                    }],
                }],
            }],
        }
        mock_get.return_value = response

        games = app.fetch_espn_fallback(week=4, year=2026)

        self.assertEqual(games[0]["wagers"], [])

    @patch("app.requests.get")
    def test_espn_fallback_uses_real_game_lines_only(self, mock_get):
        response = Mock()
        response.json.return_value = {
            "week": {"number": 4},
            "events": [{
                "id": "event-1",
                "name": "Away at Home",
                "date": "2026-10-01T20:00:00Z",
                "competitions": [{
                    "competitors": [
                        {"homeAway": "away", "team": {"displayName": "Away Team", "abbreviation": "AWY"}},
                        {"homeAway": "home", "team": {"displayName": "Home Team", "abbreviation": "HME"}},
                    ],
                    "odds": [{
                        "provider": {"name": "DraftKings"},
                        "moneyline": {
                            "home": {"close": {"odds": "-110"}},
                            "away": {"close": {"odds": "-110"}},
                        },
                        "pointSpread": {
                            "home": {"close": {"line": "+1.5", "odds": "-105"}},
                            "away": {"close": {"line": "-1.5", "odds": "-115"}},
                        },
                        "total": {
                            "over": {"close": {"line": "o41.5", "odds": "-110"}},
                            "under": {"close": {"line": "u41.5", "odds": "-110"}},
                        },
                    }],
                }],
            }],
        }
        mock_get.return_value = response

        games = app.fetch_espn_fallback(week=4, year=2026)

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["kickoff_utc"], "2026-10-01T20:00:00Z")
        self.assertEqual(len(games[0]["wagers"]), 6)
        self.assertEqual({wager["market"] for wager in games[0]["wagers"]}, {"Moneyline", "Spread", "Total"})
        self.assertEqual(games[0]["wagers"][-2]["target"], "Over 41.5")
        self.assertAlmostEqual(sum(w["fair_market_prob"] for w in games[0]["wagers"][:2]), 1.0)
        self.assertFalse(any("player" in wager["target"].lower() for wager in games[0]["wagers"]))
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs["params"], {"week": 4, "seasontype": 2, "year": 2026})


if __name__ == "__main__":
    unittest.main()
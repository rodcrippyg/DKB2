import math
import os
import sys
from datetime import datetime
from typing import Dict, List, Any
from curl_cffi import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

DK_EVENT_GROUP_ID = os.getenv("DK_EVENT_GROUP_ID", "88808")
DK_API_URL = f"https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/{DK_EVENT_GROUP_ID}?format=json"
ESPN_NFL_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

HEADERS = {
    "Accept": "application/json",
    "Referer": "https://sportsbook.draftkings.com/",
    "Origin": "https://sportsbook.draftkings.com",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


# --- 1. DATA INGESTION ENGINE ---

def fetch_live_dk_payload() -> Dict[str, Any]:
    """Fetches DraftKings event group using curl_cffi to match Chrome TLS fingerprint."""
    try:
        # impersonate="chrome124" mimics Chrome's exact JA3 TLS handshake and cipher suite
        res = requests.get(
            DK_API_URL,
            headers=HEADERS,
            impersonate="chrome124",
            timeout=15
        )
        res.raise_for_status()
        return res.json()
    except Exception as e:
        console.print(f"[yellow][!] DraftKings direct TLS notice: {e}[/yellow]")
        return {}


def fetch_espn_fallback(
    week: int | None = None,
    year: int | None = None,
    full_season: bool = False,
) -> List[Dict[str, Any]]:
    """Load the real NFL schedule without fabricating sportsbook markets."""
    try:
        if full_season:
            season_year = year or datetime.now().year
            season_payload = None
            events_by_id = {}
            for calendar_year in (season_year, season_year + 1):
                response = requests.get(
                    ESPN_NFL_URL,
                    params={"dates": str(calendar_year), "seasontype": 2, "limit": 1000},
                    impersonate="chrome124",
                    timeout=10,
                )
                response.raise_for_status()
                payload = response.json()
                if calendar_year == season_year:
                    season_payload = payload
                for event in payload.get("events", []):
                    if (event.get("season", {}).get("year") == season_year
                            and event.get("season", {}).get("type") == 2):
                        events_by_id[str(event.get("id", ""))] = event
            data = season_payload or {}
            events = list(events_by_id.values())
        else:
            params = {}
            if week is not None:
                params["week"] = week
                params["seasontype"] = 2
            if year is not None:
                params["year"] = year
            response = requests.get(ESPN_NFL_URL, params=params, impersonate="chrome124", timeout=10)
            response.raise_for_status()
            data = response.json()
            events = data.get("events", [])
        fallback_games = []

        for evt in events:
            comp = evt["competitions"][0]
            home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
            away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            wagers = []
            event_id = str(evt.get("id", ""))
            odds_feeds = comp.get("odds", [])
            draftkings_odds = next(
                (item for item in odds_feeds if "draftkings" in item.get("provider", {}).get("name", "").lower()),
                {},
            )

            def add_two_way_market(market_name: str, outcomes: List[tuple[str, str, Any]]) -> None:
                parsed = []
                for side, selection, quote in outcomes:
                    if not quote:
                        return
                    try:
                        odds_value = float(str(quote.get("odds", "")).replace("+", ""))
                        american_to_implied(odds_value)
                    except (TypeError, ValueError):
                        return
                    parsed.append((side, selection, odds_value))
                fair_probabilities = devig_market([item[2] for item in parsed])
                for (side, selection, odds_value), fair_probability in zip(parsed, fair_probabilities):
                    wagers.append({
                        "side": side,
                        "market": market_name,
                        "target": selection,
                        "odds": odds_value,
                        "implied_prob": american_to_implied(odds_value),
                        "fair_market_prob": fair_probability,
                        "source": draftkings_odds.get("provider", {}).get("name", "DraftKings via ESPN"),
                    })

            home_name = home["team"]["displayName"]
            away_name = away["team"]["displayName"]
            event_week = evt.get("week", {}).get("number") or data.get("week", {}).get("number")
            event_year = evt.get("season", {}).get("year") or year or data.get("season", {}).get("year")
            home_abbr = home["team"].get("abbreviation", "HOME")
            away_abbr = away["team"].get("abbreviation", "AWAY")
            for market_key, market_label, side_key, selection_builder in (
                ("moneyline", "Moneyline", "moneyline", lambda team, quote: team),
                ("pointSpread", "Spread", "spread", lambda team, quote: f"{team} {quote.get('line', '')}"),
            ):
                market = draftkings_odds.get(market_key, {})
                if market:
                    add_two_way_market(market_label, [
                        ("HOME", selection_builder(home_name, market.get("home", {}).get("close", {})), market.get("home", {}).get("close")),
                        ("AWAY", selection_builder(away_name, market.get("away", {}).get("close", {})), market.get("away", {}).get("close")),
                    ])

            total_market = draftkings_odds.get("total", {})
            if total_market:
                over_line = str(total_market.get("over", {}).get("close", {}).get("line", "")).removeprefix("o")
                under_line = str(total_market.get("under", {}).get("close", {}).get("line", "")).removeprefix("u")
                add_two_way_market("Total", [
                    ("BOTH", f"Over {over_line}", total_market.get("over", {}).get("close")),
                    ("BOTH", f"Under {under_line}", total_market.get("under", {}).get("close")),
                ])
            
            fallback_games.append({
                "event_id": evt.get("id", ""),
                "season_year": event_year,
                "name": evt.get("name"),
                "away": away_name,
                "home": home_name,
                "start": evt.get("date", "")[:16].replace("T", " "),
                "week": event_week,
                "is_current_week": event_week == data.get("week", {}).get("number"),
                "away_team": away_name,
                "away_abbr": away_abbr,
                "away_score": away.get("score"),
                "home_team": home_name,
                "home_abbr": home_abbr,
                "home_score": home.get("score"),
                "status": comp.get("status", {}).get("type", {}).get("shortDetail", "Scheduled"),
                "completed": bool(comp.get("status", {}).get("type", {}).get("completed")),
                "division": f"Week {event_week}" if event_week else "NFL",
                "venue": comp.get("venue", {}).get("fullName", "Venue unavailable"),
                "wagers": wagers,
            })
        return sorted(fallback_games, key=lambda game: game.get("start", ""))
    except Exception as e:
        console.print(f"[red][!] Fallback error: {e}[/red]")
        return []


# --- 2. DE-VIG & PROBABILITY MATH ---

def american_to_implied(odds_val: float) -> float:
    if not math.isfinite(odds_val) or odds_val == 0:
        raise ValueError("American odds must be a finite, non-zero number")
    if odds_val > 0:
        return 100.0 / (odds_val + 100.0)
    return abs(odds_val) / (abs(odds_val) + 100.0)


def devig_market(odds: List[float]) -> List[float]:
    """Normalize the actual outcomes listed together in one sportsbook market."""
    implied_probabilities = [american_to_implied(value) for value in odds]
    overround = sum(implied_probabilities)
    if overround <= 0:
        raise ValueError("Market outcomes must have positive implied probability")
    return [probability / overround for probability in implied_probabilities]


# --- 4. GAME DATA PARSER ---

def build_game_records(dk_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not dk_payload:
        return []

    events = dk_payload.get("eventGroup", {}).get("events", [])
    events_map = {e["eventId"]: e for e in events}
    games = {}

    for cat in dk_payload.get("eventGroup", {}).get("offerCategories", []):
        for subcat in cat.get("offerSubcategoryDescriptors", []):
            for row in subcat.get("offerSubcategory", {}).get("offers", []):
                for offer in row:
                    eid = offer.get("eventId")
                    if eid not in events_map:
                        continue
                    
                    e_info = events_map[eid]
                    g_name = e_info.get("name", "Unknown Game")
                    away_team = e_info.get("teamName2", "Away")
                    home_team = e_info.get("teamName1", "Home")

                    if g_name not in games:
                        games[g_name] = {
                            "name": g_name,
                            "away": away_team,
                            "home": home_team,
                            "start": e_info.get("startDate", "")[:16].replace("T", " "),
                            "wagers": []
                        }

                    label = offer.get("label", "Market")
                    outcomes = offer.get("outcomes", [])
                    parsed_outcomes = []
                    for out in outcomes:
                        raw_odds = str(out.get("oddsAmerican", "-110")).replace("+", "").replace("\u2212", "-")
                        try:
                            odds_val = float(raw_odds)
                            american_to_implied(odds_val)
                        except (TypeError, ValueError):
                            continue
                        parsed_outcomes.append((out, odds_val))

                    if not parsed_outcomes:
                        continue

                    market_probabilities = devig_market([odds for _, odds in parsed_outcomes])
                    for (out, odds_val), fair_market_prob in zip(parsed_outcomes, market_probabilities):

                        participant = out.get("participant") or out.get("label") or ""
                        line = str(out.get("line", ""))
                        desc = f"{participant} {line}".strip()

                        side = "HOME" if home_team.lower() in desc.lower() else "AWAY" if away_team.lower() in desc.lower() else "BOTH"

                        games[g_name]["wagers"].append({
                            "side": side,
                            "market": label,
                            "target": f"{desc} ({out.get('label', '')})",
                            "odds": odds_val,
                            "implied_prob": american_to_implied(odds_val),
                            "fair_market_prob": fair_market_prob,
                        })

    return list(games.values())


# --- 5. TERMINAL UI & INTERACTION LOOP ---

def display_dashboard(game: Dict[str, Any], perspective: str):
    console.clear()
    
    header_text = f"[bold white]NFL MARKET MONITOR[/bold white] | [cyan]{game['name']}[/cyan] ({game['start']})\n"
    header_text += f"Perspective: [bold yellow]{perspective}[/bold yellow] | Model edge: unavailable (not validated)"
    console.print(Panel(header_text, style="blue"))

    table = Table(show_header=True, header_style="bold magenta", expand=True)
    table.add_column("Side", width=8)
    table.add_column("Market", width=14)
    table.add_column("Selection", width=26)
    table.add_column("Odds", width=8)
    table.add_column("Implied", width=10)
    table.add_column("No-Vig Market", width=14)
    table.add_column("Model Edge", width=14)

    visible_wagers = []
    for bet in game["wagers"]:
        if perspective != "BOTH" and bet["side"] != perspective and bet["side"] != "BOTH":
            continue

        visible_wagers.append(bet)

    if not visible_wagers:
        console.print("[yellow]No live DraftKings markets are available for this matchup. No picks are being generated.[/yellow]")

    for bet in visible_wagers:
        side_color = "green" if bet["side"] == "HOME" else "cyan" if bet["side"] == "AWAY" else "white"

        table.add_row(
            f"[{side_color}]{bet['side']}[/{side_color}]",
            bet["market"],
            bet["target"],
            f"{bet['odds']:+.0f}",
            f"{bet['implied_prob'] * 100:.1f}%",
            f"{bet['fair_market_prob'] * 100:.1f}%",
            "Not modeled"
        )

    console.print(table)
    console.print("\n[bold]Controls:[/bold] [1] Away Only | [2] Both Teams | [3] Home Only | [n] Next Game | [r] Reset All | [q] Quit")


def run_bot():
    console.print("[cyan][*] Initializing browser-impersonated session...[/cyan]")
    dk_payload = fetch_live_dk_payload()
    games = build_game_records(dk_payload)

    if not games:
        console.print("[yellow][*] Loading live NFL schedule & market baselines from fallback source...[/yellow]")
        games = fetch_espn_fallback()

    if not games:
        console.print("[red][!] No games could be loaded. Check internet connection.[/red]")
        sys.exit(1)

    game_idx = 0
    perspective = "BOTH"
    while True:
        current_game = games[game_idx]
        display_dashboard(current_game, perspective)

        cmd = input("\nEnter Command: ").strip().lower()
        if cmd == "1":
            perspective = "AWAY"
        elif cmd == "2":
            perspective = "BOTH"
        elif cmd == "3":
            perspective = "HOME"
        elif cmd == "n":
            game_idx = (game_idx + 1) % len(games)
        elif cmd == "r":
            perspective = "BOTH"
        elif cmd == "q":
            console.print("[yellow]Exiting betting engine.[/yellow]")
            break


if __name__ == "__main__":
    run_bot()
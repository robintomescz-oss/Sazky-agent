import os
import json
import re
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv
from json_repair import repair_json
from openai import OpenAI

load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
HISTORY_FILE = "tickets_history.json"
BASE_STAKE = 1000.0

openrouter_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
) if OPENROUTER_API_KEY else None


def parse_and_repair_json(text):
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        raw_json_str = match.group(0) if match else text
        repaired = repair_json(raw_json_str, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
    except Exception as e:
        print(f"Chyba při opravě JSON: {e}")
    raise ValueError("Nepodařilo se extrahovat platný JSON objekt.")


def fetch_real_odds_matches():
    if not ODDS_API_KEY:
        return []

    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds"
    params = {
        "apiKey": ODDS_API_KEY.strip(),
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal"
    }

    try:
        res = requests.get(url, params=params, timeout=12)
        if res.status_code != 200:
            print(f"The Odds API vrátilo status {res.status_code}: {res.text}")
            return []

        remaining = res.headers.get("x-requests-remaining", "neznámo")
        print(f"The Odds API OK (zbývající kredity: {remaining}).")

        events = res.json()
        now_utc = datetime.now(timezone.utc)
        limit_utc = now_utc + timedelta(hours=48)
        candidates = []

        for ev in events:
            sport_key = ev.get("sport_key", "")
            if not sport_key.startswith("soccer_"):
                continue

            commence_str = ev.get("commence_time", "")
            try:
                commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                if commence_dt < now_utc or commence_dt > limit_utc:
                    continue
            except Exception:
                continue

            home = ev.get("home_team", "")
            away = ev.get("away_team", "")
            league = ev.get("sport_title", "Fotbal")
            bookmakers = ev.get("bookmakers", [])
            if not bookmakers:
                continue

            home_odds_list = []
            draw_odds_list = []
            away_odds_list = []

            for bm in bookmakers:
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != "h2h":
                        continue
                    outcomes = {o["name"]: float(o["price"]) for o in mkt.get("outcomes", [])}
                    if home in outcomes and away in outcomes and "Draw" in outcomes:
                        home_odds_list.append(outcomes[home])
                        away_odds_list.append(outcomes[away])
                        draw_odds_list.append(outcomes["Draw"])

            if not home_odds_list:
                continue

            avg_home = round(sum(home_odds_list) / len(home_odds_list), 2)
            avg_draw = round(sum(draw_odds_list) / len(draw_odds_list), 2)
            avg_away = round(sum(away_odds_list) / len(away_odds_list), 2)
            double_chance_1x = round(1.0 / ((1.0 / avg_home) + (1.0 / avg_draw)), 2)

            if 1.42 <= avg_home <= 1.65:
                candidates.append({
                    "home": home,
                    "away": away,
                    "league": league,
                    "utcDate": commence_str[:16].replace("T", " "),
                    "recommended_selection": "Výhra domácích (1)",
                    "real_odds": avg_home,
                    "market_context": f"1: {avg_home} | X: {avg_draw} | 2: {avg_away}"
                })
            elif 1.42 <= double_chance_1x <= 1.65:
                candidates.append({
                    "home": home,
                    "away": away,
                    "league": league,
                    "utcDate": commence_str[:16].replace("T", " "),
                    "recommended_selection": "Neprohra domácích (1X)",
                    "real_odds": double_chance_1x,
                    "market_context": f"1: {avg_home} | X: {avg_draw} | 2: {avg_away} | 1X: {double_chance_1x}"
                })

        print(f"The Odds API: nalezeno {len(candidates)} zápasů v pásmu 1.42–1.65.")
        return candidates[:20]

    except Exception as e:
        print(f"Chyba při komunikaci s The Odds API: {e}")
        return []


def fetch_espn_fallback_matches():
    matches = []
    endpoints = [
        ("UEFA Nations League", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.nations/scoreboard"),
        ("Premier League", "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"),
        ("La Liga", "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard"),
        ("Serie A", "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard"),
        ("Bundesliga", "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard"),
        ("Ligue 1", "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/scoreboard"),
        ("Champions League", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard"),
    ]

    for league_name, url in endpoints:
        try:
            res = requests.get(url, timeout=8)
            if res.status_code != 200:
                continue
            for ev in res.json().get("events", []):
                if ev.get("status", {}).get("type", {}).get("state", "") != "pre":
                    continue
                comps = ev.get("competitions", [])
                if not comps:
                    continue
                competitors = comps[0].get("competitors", [])
                if len(competitors) != 2:
                    continue
                home = next((c["team"]["displayName"] for c in competitors if c.get("homeAway") == "home"), competitors[0]["team"]["displayName"])
                away = next((c["team"]["displayName"] for c in competitors if c.get("homeAway") == "away"), competitors[1]["team"]["displayName"])
                matches.append({
                    "home": home,
                    "away": away,
                    "league": league_name,
                    "utcDate": ev.get("date", "")[:16].replace("T", " "),
                    "status": "SCHEDULED"
                })
        except Exception:
            continue
    return matches


def fetch_matches():
    odds_matches = fetch_real_odds_matches()
    if len(odds_matches) >= 2:
        return odds_matches, True

    print("Nedostatek zápasů z The Odds API, přepínám na záložní ESPN rozpis...")
    espn_matches = fetch_espn_fallback_matches()
    return espn_matches[:20], False


def get_current_stake_info():
    if not os.path.exists(HISTORY_FILE):
        return 1, int(BASE_STAKE), "Krok 1/4 (Základní vklad 1 000 Kč)"
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
            history = json.load(f)
    except Exception:
        return 1, int(BASE_STAKE), "Krok 1/4 (Základní vklad 1 000 Kč)"

    evaluated = [t for t in history if t.get("status") in ["won", "lost"]]
    step = 1
    next_stake = BASE_STAKE
    k2_

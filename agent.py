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
    """Bezpečně extrahuje a opraví JSON z odpovědi modelu."""
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
    """Stáhne nadcházející fotbalové zápasy s reálnými evropskými kurzy z The Odds API."""
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
            # Bereme pouze fotbalové soutěže
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

            # Průměr kurzů napříč dostupnými EU bookmakery pro maximální přesnost vůči Tipsportu
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

            # Výpočet reálného kurzu na neprohru domácích (1X)
            double_chance_1x = round(1.0 / ((1.0 / avg_home) + (1.0 / avg_draw)), 2)

            # Vybereme zápasy, kde buď Výhra domácích (1), nebo Neprohra domácích (1X) sedí do pásma 1.30 - 1.52
            if 1.30 <= avg_home <= 1.52:
                candidates.append({
                    "home": home,
                    "away": away,
                    "league": league,
                    "utcDate": commence_str[:16].replace("T", " "),
                    "recommended_selection": "Výhra domácích (1)",
                    "real_odds": avg_home,
                    "market_context": f"1: {avg_home} | X: {avg_draw} | 2: {avg_away}"
                })
            elif 1.30 <= double_chance_1x <= 1.52:
                candidates.append({
                    "home": home,
                    "away": away,
                    "league": league,
                    "utcDate": commence_str[:16].replace("T", " "),
                    "recommended_selection": "Neprohra domácích (1X)",
                    "real_odds": double_chance_1x,
                    "market_context": f"1: {avg_home} | X: {avg_draw} | 2: {avg_away} | 1X: {double_chance_1x}"
                })

        print(f"The Odds API: nalezeno {len(candidates)} zápasů s reálným kurzem v pásmu 1.30–1.52.")
        return candidates[:20]

    except Exception as e:
        print(f"Chyba při komunikaci s The Odds API: {e}")
        return []


def fetch_espn_fallback_matches():
    """Záložní zdroj rozpisu zápasů přes veřejné ESPN API bez kurzů."""
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
    """Hlavní funkce pro získání zápasů: primárně reálné kurzy z The Odds API, záložně ESPN."""
    odds_matches = fetch_real_odds_matches()
    if len(odds_matches) >= 2:
        return odds_matches, True

    print("Nedostatek zápasů z The Odds API, přepínám na záložní ESPN rozpis...")
    espn_matches = fetch_espn_fallback_matches()
    return espn_matches[:20], False


def get_current_stake_info():
    """Spočítá aktuální krok progrese a doporučený vklad z historie."""
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
    k2_payout = 0.0

    for t in evaluated:
        won = (t["status"] == "won")
        try:
            odds = float(t.get("total_odds", 2.0))
        except (ValueError, TypeError):
            odds = 2.0

        if step == 1:
            if won:
                net_profit = BASE_STAKE * (odds - 1.0)
                next_stake = round(net_profit / 2.0, 2)
                step = 2
            else:
                next_stake = BASE_STAKE
                step = 1
        elif step == 2:
            if won:
                k2_payout = round(next_stake * odds, 2)
                next_stake = k2_payout
                step = 3
            else:
                next_stake = BASE_STAKE
                step = 1
        elif step == 3:
            if won:
                next_stake = round(k2_payout / 2.0, 2)
                step = 4
            else:
                next_stake = BASE_STAKE
                step = 1
        elif step == 4:
            next_stake = BASE_STAKE
            step = 1

    rounded = int(round(next_stake))
    labels = {
        1: f"Krok 1/4 (Základní vklad {rounded:,} Kč)",
        2: f"Krok 2/4 (Pojistka {rounded:,} Kč – 50 % zisku z K1)",
        3: f"Krok 3/4 (Sázka {rounded:,} Kč – celá výhra z K2)",
        4: f"Krok 4/4 (Jistící sázka {rounded:,} Kč – 50 % z K2)"
    }
    return step, rounded, labels[step]


def ask_gemini(system_prompt, user_prompt):
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                temperature=0.2,
                max_output_tokens=1500,
            ),
        )
        if response and response.text:
            return parse_and_repair_json(response.text)
    except Exception as e:
        print(f"Gemini API selhalo ({e}), přecházím na zálohu...")
    return None


def ask_openrouter(system_prompt, user_prompt):
    if not openrouter_client:
        return None

    models = [
        "openrouter/auto",
        "google/gemini-2.0-flash-001",
        "meta-llama/llama-3.3-70b-instruct:free"
    ]

    for model_name in models:
        try:
            print(f"Zkouším záložní model OpenRouter: {model_name}...")
            res = openrouter_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=1200,
                temperature=0.2
            )
            if res and res.choices and res.choices[0].message.content:
                return parse_and_repair_json(res.choices[0].message.content)
        except Exception as e:
            print(f"Model {model_name} selhal ({e})...")
            continue
    return None


def generate_ticket(matches, has_real_odds):
    today = datetime.now().strftime("%Y-%m-%d")

    if not matches or len(matches) < 2:
        print("Nedostatek zápasů pro Double AKU.")
        return None

    system_prompt = "Jsi konzervativní sportovní analytik zaměřený na fotbal. Odpovídej výhradně ve validním formátu JSON."

    if has_real_odds:
        user_prompt = f"""
Dnešní datum je: {today}.

Zde je seznam nadcházejících zápasů s REÁLNÝMI EVROPSKÝMI KURZY (odpovídající Tipsportu):
{json.dumps(matches, ensure_ascii=False, indent=2)}

Vyber PŘÍSNĚ 2 NEJBEZPEČNĚJŠÍ ZÁPASY do Double AKU tiketu:
- Použij PŘESNĚ hodnotu 'recommended_selection' a PŘESNÉ číslo 'real_odds' uvedené u vybraných zápasů (nic nevymýšlej ani neupravuj).
- Celkový kurz ('total_odds') vypočítej jako přesný součin obou kurzů (zaokrouhleno na 2 desetinná místa), cíl je kolem 1.85 až 2.25.
- Preferuj známější evropské soutěže a favority s jasnou převahou.

Vrať VÝHRADNĚ platný JSON v této struktuře:
{{
  "ticket": [
    {{
      "match": "Domácí vs Hosté",
      "league": "Název ligy",
      "selection": "Výhra domácích (1) / Neprohra domácích (1X)",
      "odds": 1.40,
      "confidence_reason": "stručný analytický důvod"
    }},
    {{
      "match": "Domácí vs Hosté",
      "league": "Název ligy",
      "selection": "Výhra domácích (1) / Neprohra domácích (1X)",
      "odds": 1.42,
      "confidence_reason": "stručný analytický důvod"
    }}
  ],
  "total_odds": 1.99,
  "analysis_summary": "krátké zhodnocení reálné nabídky"
}}
"""
    else:
        user_prompt = f"""
Dnešní datum je: {today}.

Zde je seznam dostupných zápasů (záložní rozpis bez kurzů):
{json.dumps(matches, ensure_ascii=False, indent=2)}

Sestav AKU tiket PŘÍSNĚ ZE 2 NEJBEZPEČNĚJŠÍCH ZÁPASŮ (Výhra domácích nebo Over 1.5, odhadovaný celkový kurz kolem 2.00).
Vrať VÝHRADNĚ platný JSON se strukturou 'ticket', 'total_odds' a 'analysis_summary'.
"""

    print("Zkouším primární motor: Google Gemini 2.5 Flash...")
    ticket_data = ask_gemini(system_prompt, user_prompt)

    if not ticket_data:
        print("Přepínám na záložní motor: OpenRouter...")
        ticket_data = ask_openrouter(system_prompt, user_prompt)

    # Pojistka: přepočítáme přesný součin kurzů v Pythonu, aby seděl na setinu
    if ticket_data and isinstance(ticket_data.get("ticket"), list) and len(ticket_data["ticket"]) == 2:
        try:
            o1 = float(ticket_data["ticket"][0]["odds"])
            o2 = float(ticket_data["ticket"][1]["odds"])
            ticket_data["total_odds"] = round(o1 * o2, 2)
        except Exception:
            pass

    return ticket_data


def save_ticket_to_history(ticket_data):
    if not ticket_data or not ticket_data.get("ticket"):
        return

    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
                history = json.load(f)
        except Exception:
            history = []

    next_id = max([t.get("id", 0) for t in history], default=0) + 1
    entry = {
        "id": next_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_odds": ticket_data.get("total_odds"),
        "status": "pending",
        "ticket": ticket_data.get("ticket"),
        "summary": ticket_data.get("analysis_summary")
    }
    history.append(entry)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def send_telegram(ticket_data, has_real_odds):
    step, stake, step_desc = get_current_stake_info()

    if not ticket_data or not ticket_data.get("ticket"):
        message = "ℹ️ *V aktuální nabídce teď nejsou žádné zápasy splňující bezpečná kurzová kritéria.*"
    else:
        source_badge = "Reálné EU kurzy (odpovídají Tipsportu)" if has_real_odds else "Orientační kurzy (záloha)"
        lines = [f"🎯 *Fotbalový Double AKU tiket*\n_{source_badge}_\n"]
        for idx, item in enumerate(ticket_data["ticket"], 1):
            lines.append(f"{idx}. ⚽ *{item.get('match', '')}* ({item.get('league', '')})")
            lines.append(f"   Tip: `{item.get('selection', '')}` | Kurz: `{item.get('odds', '')}`")
            lines.append(f"   Důvod: _{item.get('confidence_reason', '')}_\n")

        lines.append(f"📊 *Celkový kurz:* `{ticket_data.get('total_odds', 'N/A')}`")
        lines.append(f"💰 *Staking:* *{step_desc}*")
        lines.append(f"💡 *Komentář:* {ticket_data.get('analysis_summary', '')}")
        message = "\n".join(lines)

    clean_token = TELEGRAM_BOT_TOKEN.replace("[", "").replace("]", "").replace("https://api.telegram.org/bot", "").strip()
    url = f"https://api.telegram.org/bot{clean_token}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID.strip(),
        "text": message,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload, timeout=10)


if __name__ == "__main__":
    print("1/4 Stahuji zápasy a reálné kurzy...")
    matches, has_real_odds = fetch_matches()
    print(f"Celkem připraveno {len(matches)} kandidátů (reálné kurzy: {has_real_odds}).")

    print("2/4 Analyzuji nabídku a sestavuji tiket...")
    ticket = generate_ticket(matches, has_real_odds)

    print("3/4 Ukládám do historie...")
    save_ticket_to_history(ticket)

    print("4/4 Odesílám na Telegram...")
    send_telegram(ticket, has_real_odds)
    print("Hotovo!")

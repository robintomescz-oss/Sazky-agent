import os
import json
import re
from datetime import datetime, timedelta
import requests
from dotenv import load_dotenv
from json_repair import repair_json
from openai import OpenAI

load_dotenv()

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


def fetch_today_matches():
    """Stáhne zápasy pro dnešek a nejbližší 2 dny z football-data.org."""
    today = datetime.now().strftime("%Y-%m-%d")
    day_plus_2 = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    url = f"https://api.football-data.org/v4/matches?dateFrom={today}&dateTo={day_plus_2}"
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY.strip()} if FOOTBALL_DATA_API_KEY else {}

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            print(f"Chyba při stahování zápasů ({res.status_code}): {res.text}")
            return []

        data = res.json()
        raw_matches = data.get("matches", [])

        # Rozšířený seznam soutěží včetně repre (EC, WC) a Championship (ELC)
        allowed_competitions = {
            "PL", "PD", "BL1", "SA", "FL1", "DED", "PPL",
            "CL", "EL", "EC", "WC", "ELC", "BSA"
        }
        matches = []

        # Nejprve vezmeme zápasy z prioritních soutěží (pouze neodehrané)
        for m in raw_matches:
            if m.get("status") not in ["SCHEDULED", "TIMED"]:
                continue
            comp_code = m.get("competition", {}).get("code")
            if comp_code in allowed_competitions:
                matches.append({
                    "home": m["homeTeam"]["name"],
                    "away": m["awayTeam"]["name"],
                    "league": m.get("competition", {}).get("name", "Fotbal"),
                    "utcDate": m.get("utcDate", "")[:16].replace("T", " "),
                    "status": m.get("status")
                })

        # Záložní fallback: pokud v hlavních soutěžích nejsou alespoň 4 zápasy, vezmeme vše dostupné v API
        if len(matches) < 4 and raw_matches:
            seen = {(x["home"], x["away"]) for x in matches}
            for m in raw_matches:
                if m.get("status") not in ["SCHEDULED", "TIMED"]:
                    continue
                pair = (m["homeTeam"]["name"], m["awayTeam"]["name"])
                if pair not in seen:
                    matches.append({
                        "home": m["homeTeam"]["name"],
                        "away": m["awayTeam"]["name"],
                        "league": m.get("competition", {}).get("name", "Fotbal"),
                        "utcDate": m.get("utcDate", "")[:16].replace("T", " "),
                        "status": m.get("status")
                    })
                    seen.add(pair)
                if len(matches) >= 15:
                    break

        return matches[:20]
    except Exception as e:
        print(f"Výjimka při stahování zápasů: {e}")
        return []


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
    """Zavolá Gemini Flash s vynuceným JSON formátem."""
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
        print(f"Gemini API selhalo nebo není dostupné ({e}), přecházím na zálohu...")
    return None


def ask_openrouter(system_prompt, user_prompt):
    """Záložní volání modelů na OpenRouteru s automatickou opravou JSONu."""
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


def generate_ticket(matches):
    today = datetime.now().strftime("%Y-%m-%d")

    # Pro Double AKU stačí minimálně 2 dostupné zápasy
    if not matches or len(matches) < 2:
        print("V nejbližších 48 hodinách není v API dostatek zápasů ani pro Double AKU.")
        return None

    system_prompt = "Jsi konzervativní sportovní analytik zaměřený na fotbal. Odpovídej výhradně ve validním formátu JSON."
    user_prompt = f"""
Dnešní datum je: {today}.

Zde je seznam dostupných reálných zápasů pro dnešek a nejbližší dny:
{json.dumps(matches, ensure_ascii=False, indent=2)}

Sestav AKU tiket PŘÍSNĚ ZE 2 NEJBEZPEČNĚJŠÍCH ZÁPASŮ (žádný třetí zápas):
1. Zápas 1: Silný favorit na domácí půdě (Výhra domácích / Neprohra domácích) NEBO otevřený ofenzivní zápas (Over 1.5). Kurz cca 1.35 až 1.48.
2. Zápas 2: Silný favorit na domácí půdě (Výhra domácích / Neprohra domácích) NEBO otevřený ofenzivní zápas (Over 1.5). Kurz cca 1.35 až 1.48.

Pravidla:
- Použij PŘESNÉ názvy týmů ze seznamu výše (Domácí vs Hosté).
- Žádné sázky na outsidery.
- Žádný třetí zápas.
- Celkový kurz tiketu cca 1.90 až 2.15 (součin kurzů).

Vrať VÝHRADNĚ platný JSON v této struktuře:
{{
  "ticket": [
    {{
      "match": "Domácí vs Hosté",
      "league": "Název ligy",
      "selection": "Výhra domácích / Over 1.5",
      "odds": 1.40,
      "confidence_reason": "stručný důvod výběru"
    }},
    {{
      "match": "Domácí vs Hosté",
      "league": "Název ligy",
      "selection": "Výhra domácích / Over 1.5",
      "odds": 1.42,
      "confidence_reason": "stručný důvod výběru"
    }}
  ],
  "total_odds": 1.99,
  "analysis_summary": "krátké zhodnocení strategie"
}}
"""

    print("Zkouším primární motor: Google Gemini 2.5 Flash...")
    ticket_data = ask_gemini(system_prompt, user_prompt)

    if not ticket_data:
        print("Přepínám na záložní motor: OpenRouter...")
        ticket_data = ask_openrouter(system_prompt, user_prompt)

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


def send_telegram(ticket_data):
    step, stake, step_desc = get_current_stake_info()

    if not ticket_data or not ticket_data.get("ticket"):
        message = "ℹ️ *Ani v nejbližších 48 hodinách není v API dostatek zápasů pro bezpečný tiket.*"
    else:
        lines = ["🎯 *Fotbalový Double AKU tiket (Cíl ~2.00)*\n"]
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
    print("1/4 Stahuji rozpis zápasů (dnes + 2 dny)...")
    matches = fetch_today_matches()
    print(f"Nalezeno {len(matches)} dostupných zápasů.")

    print("2/4 Analyzuji nabídku a sestavuji tiket...")
    ticket = generate_ticket(matches)

    print("3/4 Ukládám do historie...")
    save_ticket_to_history(ticket)

    print("4/4 Odesílám na Telegram...")
    send_telegram(ticket)
    print("Hotovo!")

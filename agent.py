import os
import json
import re
from datetime import datetime
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

# Inicializace klienta OpenRouter
openrouter_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
) if OPENROUTER_API_KEY else None

def parse_and_repair_json(text):
    """Bezpečně extrahuje a opraví JSON z odpovědi modelu."""
    try:
        # Hledáme první { a poslední }
        match = re.search(r"\{.*\}", text, re.DOTALL)
        raw_json_str = match.group(0) if match else text
        repaired = repair_json(raw_json_str, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
    except Exception as e:
        print(f"Chyba při opravě JSON: {e}")
    raise ValueError("Nepodařilo se extrahovat platný JSON objekt.")

def fetch_today_matches():
    """Stáhne dnešní zápasy z football-data.org."""
    url = "https://api.football-data.org/v4/matches"
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY.strip()} if FOOTBALL_DATA_API_KEY else {}

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            print(f"Chyba při stahování zápasů ({res.status_code}): {res.text}")
            return []

        data = res.json()
        raw_matches = data.get("matches", [])
        
        allowed_competitions = {"PL", "PD", "BL1", "SA", "FL1", "DED", "PPL", "CL", "EL"}
        matches = []

        for m in raw_matches:
            comp_code = m.get("competition", {}).get("code")
            if comp_code in allowed_competitions:
                matches.append({
                    "home": m["homeTeam"]["name"],
                    "away": m["awayTeam"]["name"],
                    "league": m.get("competition", {}).get("name", "Fotbal"),
                    "status": m.get("status")
                })

        if not matches and raw_matches:
            for m in raw_matches[:12]:
                matches.append({
                    "home": m["homeTeam"]["name"],
                    "away": m["awayTeam"]["name"],
                    "league": m.get("competition", {}).get("name", "Fotbal"),
                    "status": m.get("status")
                })

        return matches
    except Exception as e:
        print(f"Výjimka při stahování zápasů: {e}")
        return []

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
        "liquid/lfm-2.5-2.6b:free",
        "nvidia/nemotron-3.5-lightning:free"
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

    if not matches:
        print("Dnes nebyly nalezeny žádné zápasy v podporovaných ligách.")
        return None

    system_prompt = "Jsi konzervativní sportovní analytik zaměřený na fotbal. Odpovídej výhradně ve validním formátu JSON."
    user_prompt = f"""
Dnešní datum je: {today}.

Zde je seznam dnešních reálných zápasů:
{json.dumps(matches, ensure_ascii=False, indent=2)}

Vyber 2 až 3 nejbezpečnější tipy výhradně z této nabídky:
- Silní favorité (Výhra domácích, odhadovaný kurz 1.25 až 1.45)
- Nebo sázky na góly: Over 1.5 gólu u týmů s vysokou ofenzivní silou.
- Celkový kurz tiketu cca 1.90 až 2.20 (musí odpovídat součinu dílčích kurzů).

Vrať VÝHRADNĚ platný JSON v této struktuře:
{{
  "ticket": [
    {{
      "match": "Domácí vs Hosté",
      "league": "Název ligy",
      "selection": "Výhra domácích / Over 1.5",
      "odds": 1.35,
      "confidence_reason": "stručný důvod výběru"
    }}
  ],
  "total_odds": 2.05,
  "analysis_summary": "krátké zhodnocení strategie pro dnešní den"
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
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []

    entry = {
        "id": len(history) + 1,
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
    if not ticket_data or not ticket_data.get("ticket"):
        message = "ℹ️ *Dnes se nehraje dostatek zápasů v top ligách pro sestavení konzervativního tiketu.*"
    else:
        lines = ["🎯 *Denní fotbalový tiket (Cíl ~2.00)*\n"]
        for idx, item in enumerate(ticket_data["ticket"], 1):
            lines.append(f"{idx}. ⚽ *{item.get('match', '')}* ({item.get('league', '')})")
            lines.append(f"   Tip: `{item.get('selection', '')}` | Kurz: `{item.get('odds', '')}`")
            lines.append(f"   Důvod: _{item.get('confidence_reason', '')}_\n")

        lines.append(f"📊 *Celkový kurz:* `{ticket_data.get('total_odds', 'N/A')}`")
        lines.append(f"💡 *Komentář:* {ticket_data.get('analysis_summary', '')}")
        message = "\n".join(lines)

    clean_token = TELEGRAM_BOT_TOKEN.replace("[", "").replace("]", "").replace("https://api.telegram.org/bot", "").strip()
    url = f"https://api.telegram.org/bot{clean_token}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID.strip(),
        "text": message,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

if __name__ == "__main__":
    print("1/4 Stahuji dnešní živý rozpis zápasů...")
    matches = fetch_today_matches()
    print(f"Nalezeno {len(matches)} zápasů v hlavních soutěžích.")

    print("2/4 Analyzuji nabídku a sestavuji tiket...")
    ticket = generate_ticket(matches)

    print("3/4 Ukládám do historie...")
    save_ticket_to_history(ticket)

    print("4/4 Odesílám na Telegram...")
    send_telegram(ticket)
    print("Hotovo!")
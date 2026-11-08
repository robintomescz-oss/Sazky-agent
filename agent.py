import os
import json
import re
from datetime import datetime
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
HISTORY_FILE = "tickets_history.json"

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

def extract_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("V odpovědi nebyl nalezen validní JSON.")

def fetch_today_matches():
    """Stáhne dnešní zápasy z hlavních evropských lig z football-data.org."""
    today = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.football-data.org/v4/matches?dateFrom={today}&dateTo={today}"
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY} if FOOTBALL_DATA_API_KEY else {}
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            matches = []
            # Filtrujeme hlavní ligy: PL, La Liga, Bundesliga, Serie A, Ligue 1, Liga mistrů
            allowed_competitions = {"PL", "PD", "BL1", "SA", "FL1", "CL", "EL"}
            for m in data.get("matches", []):
                comp = m.get("competition", {}).get("code")
                if comp in allowed_competitions or not FOOTBALL_DATA_API_KEY:
                    matches.append({
                        "home": m["homeTeam"]["name"],
                        "away": m["awayTeam"]["name"],
                        "league": m.get("competition", {}).get("name", "Fotbal"),
                        "status": m.get("status")
                    })
            return matches
        else:
            print(f"Chyba při stahování zápasů ({res.status_code}): {res.text}")
            return []
    except Exception as e:
        print(f"Výjimka při stahování zápasů: {e}")
        return []

def generate_ticket(matches):
    today = datetime.now().strftime("%Y-%m-%d")

    if not matches:
        print("Dnes nebyly nalezeny žádné zápasy v podporovaných ligách.")
        return None

    prompt = f"""
Jsi konzervativní sportovní analytik zaměřený na fotbal.
Dnešní datum je: {today}.

Zde je seznam dnešních reálných zápasů:
{json.dumps(matches, ensure_ascii=False, indent=2)}

Vyber 2 až 3 nejbezpečnější tipy výhradně z této nabídky:
- Silní favorité (Výhra domácích, odhadovaný kurz 1.25 až 1.45)
- Nebo sázky na góly: Over 1.5 gólu u týmů s vysokou ofenzivní silou.
- Celkový kurz tiketu cca 1.90 až 2.20 (musí odpovídat součinu dílčích kurzů).

Vrať VÝHRADNĚ platný JSON (bez jakýchkoliv značek nebo dalšího textu):
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
    free_models = []
    try:
        models_data = client.models.list().data
        free_models = [m.id for m in models_data if ":free" in m.id]
    except Exception:
        pass

    if "openrouter/auto" not in free_models:
        free_models.append("openrouter/auto")

    ticket_data = None
    for model_name in free_models:
        try:
            print(f"Zkouším model: {model_name}...")
            res = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "Jsi konzervativní sportovní analytik. Odpovídej výhradně ve validním formátu JSON."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1200,
                temperature=0.2
            )
            if res and res.choices and res.choices[0].message.content:
                ticket_data = extract_json(res.choices[0].message.content.strip())
                print(f"Úspěšně sestaven tiket modelem: {model_name}")
                break
        except Exception as e:
            print(f"Model {model_name} selhal ({e}), zkouším další...")
            continue

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
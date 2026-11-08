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
HISTORY_FILE = "tickets_history.json"

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

def extract_json(text):
    """Vytáhne čistý JSON objekt z libovolného textového výstupu modelu."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("V odpovědi modelu nebyl nalezen validní JSON blok.")

def generate_ticket():
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""
Jsi konzervativní sportovní analytik zaměřený na fotbal.
Dnešní datum je: {today}.

Sestav konzervativní tiket na dnešní zápasy:
- 2 až 3 tipy s co nejvyšší pravděpodobností úspěchu.
- Celkový kurz tiketu cca 1.90 až 2.20.
- Pravidla:
  * Silní domácí favorité v evropských ligách (kurz 1.25 až 1.45).
  * Nebo sázky na góly: Over 1.5 gólu u ofenzivních týmů.
- Vyber 2 tipy (kurzy ~1.40) NEBO 3 tipy (kurzy ~1.25).
- Celkový kurz musí odpovídat součinu kurzů.

Vrať VÝHRADNĚ platný JSON (bez jakéhokoliv dalšího úvodního či závěrečného textu):
{{
  "ticket": [
    {{
      "match": "Domácí vs Hosté",
      "league": "Název ligy",
      "selection": "Výhra domácích / Over 1.5",
      "odds": 1.32,
      "confidence_reason": "stručný důvod výběru"
    }}
  ],
  "total_odds": 2.05,
  "analysis_summary": "krátké zhodnocení strategie"
}}
"""
    # Získání bezplatných modelů
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
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "Jsi profesionální sportovní analytik. Odpovídej výhradně ve validním formátu JSON."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1200,  # Dostatek prostoru pro celý tiket bez uříznutí
                temperature=0.2
            )
            
            if response and response.choices and response.choices[0].message.content:
                raw_text = response.choices[0].message.content.strip()
                ticket_data = extract_json(raw_text)
                print(f"Úspěšně sestaven tiket modelem: {model_name}")
                break
        except Exception as e:
            print(f"Model {model_name} selhal ({e}), zkouším další...")
            continue

    if not ticket_data:
        raise Exception("Nepodařilo se vygenerovat kompletní tiket z žádného modelu.")
    
    return ticket_data

def save_ticket_to_history(ticket_data):
    if not ticket_data.get("ticket"):
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
    if not ticket_data.get("ticket"):
        message = "⚠️ Dnes se nepodařilo sestavit tiket s dostatečnou důvěrou."
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
    res = requests.post(url, json=payload)
    if res.status_code != 200:
        print(f"Chyba Telegramu ({res.status_code}): {res.text}")

if __name__ == "__main__":
    print("1/3 Analyzuji dnešní nabídku...")
    ticket = generate_ticket()
    print("2/3 Ukládám tiket do historie...")
    save_ticket_to_history(ticket)
    print("3/3 Odesílám na Telegram...")
    send_telegram(ticket)
    print("Hotovo! Zkontrolujte Telegram.")
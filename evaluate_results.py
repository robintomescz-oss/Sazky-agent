import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
HISTORY_FILE = "tickets_history.json"

def fetch_api_matches():
    """Stáhne aktuální výsledky zápasů z football-data.org."""
    url = "https://api.football-data.org/v4/matches"
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY.strip()} if FOOTBALL_DATA_API_KEY else {}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            return res.json().get("matches", [])
        print(f"API vrátilo status {res.status_code}: {res.text}")
    except Exception as e:
        print(f"Chyba při stahování výsledků z API: {e}")
    return []

def normalize_name(name):
    name = name.lower()
    for clean in ["fc", "cf", "ssc", "ss", "ac", "sc", "1907", "de", "hove & albion", "&"]:
        name = name.replace(clean, "")
    return "".join(c for c in name if c.isalnum())

def find_match_result(home_target, away_target, api_matches):
    h_norm = normalize_name(home_target)
    a_norm = normalize_name(away_target)

    for m in api_matches:
        api_h = normalize_name(m["homeTeam"]["name"])
        api_a = normalize_name(m["awayTeam"]["name"])

        if (h_norm in api_h or api_h in h_norm) and (a_norm in api_a or api_a in a_norm):
            status = m.get("status")
            score_data = m.get("score", {}).get("fullTime", {})
            return status, score_data.get("home"), score_data.get("away")

    return None, None, None

def evaluate_selection(selection, home_goals, away_goals):
    sel = selection.lower()
    if home_goals is None or away_goals is None:
        return False
    total_goals = home_goals + away_goals

    if "over 1.5" in sel:
        return total_goals >= 2
    if "výhra domácích" in sel or sel.strip() == "1":
        return home_goals > away_goals
    if "výhra hostů" in sel or sel.strip() == "2":
        return away_goals > home_goals
    if "neprohra domácích" in sel or "1x" in sel:
        return home_goals >= away_goals
    if "neprohra hostů" in sel or "x2" in sel:
        return away_goals >= home_goals
    if "remíza" in sel or sel.strip() == "x":
        return home_goals == away_goals
    if "1.5" in sel and total_goals >= 2:
        return True

    return False

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    clean_token = TELEGRAM_BOT_TOKEN.replace("[", "").replace("]", "").replace("https://api.telegram.org/bot", "").strip()
    url = f"https://api.telegram.org/bot{clean_token}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID.strip(),
        "text": message,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def evaluate_pending_tickets():
    if not os.path.exists(HISTORY_FILE):
        print("Soubor historie neexistuje.")
        return

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)

    pending_tickets = [t for t in history if t.get("status") == "pending"]
    if not pending_tickets:
        print("Všechny tikety jsou již vyhodnoceny (žádný status 'pending').")
        return

    api_matches = fetch_api_matches()
    print(f"Staženo {len(api_matches)} zápasů z API pro kontrolu.")

    updated_any = False

    for t in pending_tickets:
        print(f"\nVyhodnocuji tiket ID {t['id']} z {t['date']}...")
        all_finished = True
        ticket_won = True
        details = []

        for item in t.get("ticket", []):
            match_name = item.get("match", "")
            selection = item.get("selection", "")
            parts = match_name.split(" vs ")
            if len(parts) != 2:
                details.append(f"⚠️ {match_name}: Neplatný formát")
                continue

            home_team, away_team = parts[0].strip(), parts[1].strip()
            status, h_goals, a_goals = find_match_result(home_team, away_team, api_matches)

            if status != "FINISHED":
                all_finished = False
                status_label = status if status else "NENALEZENO / JEŠTĚ NEHRÁNO"
                details.append(f"⏳ {match_name}: {status_label}")
                continue

            item_won = evaluate_selection(selection, h_goals, a_goals)
            icon = "✅" if item_won else "❌"
            details.append(f"{icon} {match_name} ({h_goals}:{a_goals}) -> {selection}")

            if not item_won:
                ticket_won = False

        if all_finished:
            t["status"] = "won" if ticket_won else "lost"
            t["evaluation_detail"] = " | ".join(details)
            updated_any = True
            
            hdr = "🎉 *VEČERNÍ VYHODNOCENÍ: TIKET VYHRÁL!*" if ticket_won else "💀 *VEČERNÍ VYHODNOCENÍ: TIKET PROHRÁL*"
            msg = f"{hdr}\n\n" + "\n".join(details) + f"\n\n📊 *Kurz:* `{t.get('total_odds')}`"
            send_telegram(msg)
            print(f"Tiket ID {t['id']} uzavřen: {t['status']}")
        else:
            print(f"Tiket ID {t['id']} zatím nelze uzavřít (zápasy ještě nebyly odehrány).")

    if updated_any:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    total_eval = [t for t in history if t.get("status") in ["won", "lost"]]
    w = sum(1 for t in total_eval if t["status"] == "won")
    l = sum(1 for t in total_eval if t["status"] == "lost")
    profit = sum(float(t.get("total_odds", 2.0)) - 1.0 for t in total_eval if t["status"] == "won") - l
    roi = (profit / len(total_eval) * 100) if total_eval else 0.0

    print("\n" + "="*40)
    print("      AKTUÁLNÍ CELKOVÁ BILANCE")
    print("="*40)
    print(f"Vyhodnoceno : {len(total_eval)}")
    print(f"Bilance     : {w}W - {l}L")
    sign = "+" if profit >= 0 else ""
    print(f"Zisk (1u)   : {sign}{profit:.2f} u")
    print(f"ROI         : {sign}{roi:.1f} %")
    print("="*40)

if __name__ == "__main__":
    evaluate_pending_tickets()

import os
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
HISTORY_FILE = "tickets_history.json"
BASE_STAKE = 1000.0  # Základní vklad v Kč pro Krok 1

def fetch_api_matches():
    """Stáhne aktuální výsledky zápasů za včerejšek i dnešek z football-data.org."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    url = f"https://api.football-data.org/v4/matches?dateFrom={yesterday}&dateTo={today}"

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
    if "neprohra domácích" in sel or "1x" in sel:
        return home_goals >= away_goals
    if "neprohra hostů" in sel or "x2" in sel:
        return away_goals >= home_goals
    if "výhra domácích" in sel or sel.strip() == "1":
        return home_goals > away_goals
    if "výhra hostů" in sel or sel.strip() == "2":
        return away_goals > home_goals
    if "1.5" in sel and total_goals >= 2:
        return True

    return False

def calculate_next_progression(history):
    """
    Deterministický stavový automat s REÁLNÝMI kurzy tiketů a pojistkou v Kroku 2:
      K1: Vklad 1 000 Kč (z kapsy). Výhra -> uzamkne 50 % čistého zisku (1000 * (odds - 1) / 2)
      K2: Vklad = zbylých 50 % čistého zisku z K1. Výhra -> celá výplata (stake * odds) do K3
      K3: Vklad = výhra z K2. Výhra -> celá výplata uzamčena do banku, 50 % výhry z K2 jde do K4
      K4: Jistící sázka. Výhra -> zisk přičten k banku, konec cyklu -> reset na K1
    """
    evaluated = [t for t in history if t.get("status") in ["won", "lost"]]
    step = 1
    total_bank_profit = 0.0

    next_stake = BASE_STAKE
    k2_payout = 0.0

    for t in evaluated:
        won = (t["status"] == "won")
        try:
            odds = float(t.get("total_odds", 2.0))
        except (ValueError, TypeError):
            odds = 2.0

        if step == 1:
            stake = BASE_STAKE
            if won:
                net_profit = stake * (odds - 1.0)
                saved_profit = round(net_profit / 2.0, 2)
                total_bank_profit += saved_profit
                next_stake = round(net_profit / 2.0, 2)
                step = 2
            else:
                total_bank_profit -= stake
                next_stake = BASE_STAKE
                step = 1

        elif step == 2:
            stake = next_stake
            if won:
                k2_payout = round(stake * odds, 2)
                next_stake = k2_payout
                step = 3
            else:
                next_stake = BASE_STAKE
                step = 1

        elif step == 3:
            stake = next_stake
            if won:
                k3_payout = round(stake * odds, 2)
                total_bank_profit += k3_payout
                next_stake = round(k2_payout / 2.0, 2)
                step = 4
            else:
                next_stake = BASE_STAKE
                step = 1

        elif step == 4:
            stake = next_stake
            if won:
                net_profit = round(stake * (odds - 1.0), 2)
                total_bank_profit += net_profit
            else:
                total_bank_profit -= stake
            next_stake = BASE_STAKE
            step = 1

    # Formátování výstupu pro následující tiket
    rounded_stake = int(round(next_stake))
    if step == 1:
        desc = "Krok 1/4 (Základní vklad 1 000 Kč z vlastní kapsy)"
    elif step == 2:
        desc = f"Krok 2/4 (Pojistka {rounded_stake} Kč – reálných 50 % zisku z K1)"
    elif step == 3:
        desc = f"Krok 3/4 (Sázka {rounded_stake} Kč – reálná celá výhra z K2)"
    elif step == 4:
        desc = f"Krok 4/4 (Jistící sázka {rounded_stake} Kč – reálných 50 % z K2)"

    return step, rounded_stake, desc, int(round(total_bank_profit))

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
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Chyba při odesílání na Telegram: {e}")

def evaluate_pending_tickets():
    if not os.path.exists(HISTORY_FILE):
        print("Soubor historie neexistuje.")
        return

    with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
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
                status_label = status if status else "NENALEZENO / NEODEHRÁNO"
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

            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)

            next_step, next_stake, step_desc, bank_profit = calculate_next_progression(history)

            hdr = "🎉 *VEČERNÍ VYHODNOCENÍ: TIKET VYHRÁL!*" if ticket_won else "💀 *VEČERNÍ VYHODNOCENÍ: TIKET PROHRÁL*"
            sign = "+" if bank_profit >= 0 else ""

            msg = (
                f"{hdr}\n\n"
                f"{chr(10).join(details)}\n\n"
                f"📊 *Reálný kurz tiketu:* `{t.get('total_odds')}`\n"
                f"------------------------------------\n"
                f"🎯 *DYNAMICKÁ STAKINGOVÁ PROGRESE:*\n"
                f"• Příští krok: *{step_desc}*\n"
                f"• Doporučená sázka zítra: *{next_stake:,} Kč*\n"
                f"• Skutečný zisk strategie: *{sign}{bank_profit:,} Kč*"
            )
            send_telegram(msg)
            print(f"Tiket ID {t['id']} uzavřen: {t['status']}")
            print(f"Příští sázka: {next_stake} Kč ({step_desc}) | Celková bilance: {bank_profit} Kč")
        else:
            print(f"Tiket ID {t['id']} zatím nelze uzavřít (zápasy ještě nebyly odehrány).")

    if updated_any:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    evaluate_pending_tickets()
        

import os
import json
import re
import unicodedata
from datetime import datetime, timedelta
import requests
from dotenv import load_dotenv

load_dotenv()

FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HISTORY_FILE = "tickets_history.json"
BASE_STAKE = 1000.0


def normalize_team_name(name):
    """Odstraní diakritiku, běžné předpony/přípony klubů a převede na malá písmena."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii").lower()
    noise_words = {
        "fc", "cf", "sc", "ac", "as", "afc", "rc", "cd", "ud", "sd", "ca", "cr", "se", "ec",
        "fk", "sk", "bk", "ik", "if", "sv", "vfb", "vfl", "tsg", "fsv", "rb", "bsc", "tsv",
        "club", "de", "futbol", "calcio", "united", "city", "town", "rovers", "wanderers",
        "athletic", "atletico", "real", "sporting", "inter", "olympique", "borussia", "dynamo"
    }
    tokens = [t for t in re.findall(r"[a-z0-9]+", text) if t not in noise_words]
    return " ".join(tokens) if tokens else text.strip()


def teams_match(query_team, api_team):
    """Porovná název týmu z tiketu s názvem v API."""
    q_norm = normalize_team_name(query_team)
    a_norm = normalize_team_name(api_team)
    if not q_norm or not a_norm:
        return False
    if q_norm == a_norm or q_norm in a_norm or a_norm in q_norm:
        return True
    q_tokens = set(q_norm.split())
    a_tokens = set(a_norm.split())
    return bool(q_tokens & a_tokens and len(max(q_tokens & a_tokens, key=len)) >= 4)


def send_telegram_notification(message):
    """Odešle zprávu na Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Chybí Telegram credentials.")
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


def sync_telegram_odds_correction(history):
    """Přečte z Telegramu poslední příkaz /kurz X.XX a aktualizuje total_odds u pending tiketu."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    clean_token = TELEGRAM_BOT_TOKEN.replace("[", "").replace("]", "").replace("https://api.telegram.org/bot", "").strip()
    url = f"https://api.telegram.org/bot{clean_token}/getUpdates"

    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            return False

        updates = res.json().get("result", [])
        latest_odds = None
        last_update_id = None

        for upd in updates:
            last_update_id = upd.get("update_id")
            msg = upd.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()

            if chat_id == str(TELEGRAM_CHAT_ID).strip() and text.lower().startswith("/kurz"):
                match = re.search(r"/kurz\s+([0-9]+[.,][0-9]+|[0-9]+)", text, re.IGNORECASE)
                if match:
                    latest_odds = float(match.group(1).replace(",", "."))

        if last_update_id is not None:
            requests.get(url, params={"offset": last_update_id + 1}, timeout=5)

        if latest_odds and latest_odds > 1.0:
            pending_tickets = [t for t in history if t.get("status") == "pending"]
            if pending_tickets:
                target = pending_tickets[-1]
                old_odds = target.get("total_odds")
                if old_odds != latest_odds:
                    target["total_odds"] = latest_odds
                    print(f"Telegram korekce: Tiket ID {target.get('id')} kurz upraven z {old_odds} na {latest_odds}.")
                    send_telegram_notification(
                        f"✏️ *Korekce kurzu potvrzena*\nTiket ID {target.get('id')}: celkový kurz upraven z `{old_odds}` na reálných `{latest_odds}`."
                    )
                    return True
    except Exception as e:
        print(f"Chyba při čtení korekce kurzu z Telegramu: {e}")

    return False


def fetch_finished_matches():
    """Stáhne dohrané zápasy za poslední 4 dny z football-data.org i ESPN."""
    finished = []
    date_from = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
    date_to = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. Primární zdroj: football-data.org
    if FOOTBALL_DATA_API_KEY:
        url = f"https://api.football-data.org/v4/matches?dateFrom={date_from}&dateTo={date_to}&status=FINISHED"
        headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY.strip()}
        try:
            res = requests.get(url, headers=headers, timeout=12)
            if res.status_code == 200:
                for m in res.json().get("matches", []):
                    ft = m.get("score", {}).get("fullTime", {})
                    if ft.get("home") is not None and ft.get("away") is not None:
                        finished.append({
                            "home": m["homeTeam"]["name"],
                            "away": m["awayTeam"]["name"],
                            "home_goals": int(ft["home"]),
                            "away_goals": int(ft["away"])
                        })
        except Exception as e:
            print(f"Chyba football-data.org při vyhodnocování: {e}")

    # 2. Záložní zdroj: ESPN API (pokryje repre, poháry, Brazílii i další soutěže)
    espn_leagues = [
        "uefa.nations", "fifa.worldq.uefa", "fifa.friendly",
        "eng.1", "eng.2", "esp.1", "ita.1", "ger.1", "fra.1",
        "ned.1", "por.1", "uefa.champions", "uefa.europa", "bra.1"
    ]
    dates_param = f"{(datetime.now() - timedelta(days=3)).strftime('%Y%m%d')}-{datetime.now().strftime('%Y%m%d')}"

    for slug in espn_leagues:
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={dates_param}"
        try:
            res = requests.get(url, timeout=8)
            if res.status_code != 200:
                continue
            for ev in res.json().get("events", []):
                state = ev.get("status", {}).get("type", {}).get("state", "")
                if state != "post":
                    continue
                comps = ev.get("competitions", [])
                if not comps:
                    continue
                competitors = comps[0].get("competitors", [])
                if len(competitors) != 2:
                    continue
                home_comp = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away_comp = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
                try:
                    h_goals = int(home_comp.get("score"))
                    a_goals = int(away_comp.get("score"))
                    finished.append({
                        "home": home_comp["team"]["displayName"],
                        "away": away_comp["team"]["displayName"],
                        "home_goals": h_goals,
                        "away_goals": a_goals
                    })
                except (ValueError, TypeError):
                    continue
        except Exception:
            continue

    return finished


def evaluate_selection(selection, home_goals, away_goals):
    """Vyhodnotí konkrétní sázkovou příležitost na základě skóre."""
    sel = selection.lower().strip()
    total_goals = home_goals + away_goals

    # Neprohra domácích (1X)
    if "1x" in sel or "neprohra domácích" in sel or "neprohra domacich" in sel or "home or draw" in sel:
        return home_goals >= away_goals

    # Neprohra hostů (X2)
    if "x2" in sel or "neprohra hostů" in sel or "neprohra hostu" in sel or "away or draw" in sel:
        return away_goals >= home_goals

    # Výhra domácích (1)
    if "výhra domácích" in sel or "vyhra domacich" in sel or sel in {"1", "home", "home win"} or "(1)" in sel:
        return home_goals > away_goals

    # Výhra hostů (2)
    if "výhra hostů" in sel or "vyhra hostu" in sel or sel in {"2", "away", "away win"} or "(2)" in sel:
        return away_goals > home_goals

    # Remíza (X)
    if "remíza" in sel or "remiza" in sel or sel in {"x", "draw"}:
        return home_goals == away_goals

    # Over / Under góly
    over_match = re.search(r"over\s*([0-9]+(?:\.[0-9]+)?)", sel)
    if over_match:
        line = float(over_match.group(1))
        return total_goals > line

    under_match = re.search(r"under\s*([0-9]+(?:\.[0-9]+)?)", sel)
    if under_match:
        line = float(under_match.group(1))
        return total_goals < line

    # Oba dají gól (BTTS)
    if "btts" in sel or "oba dají gól" in sel or "oba daji gol" in sel:
        return home_goals > 0 and away_goals > 0

    return None


def calculate_progression_state(history):
    """Spočítá kompletní stav 4krokové progrese, čistý zisk banku a vklad pro příští krok."""
    evaluated = [t for t in history if t.get("status") in ["won", "lost"]]
    step = 1
    next_stake = BASE_STAKE
    k2_payout = 0.0
    total_bank_profit = 0.0

    for t in evaluated:
        won = (t["status"] == "won")
        try:
            odds = float(t.get("total_odds", 2.0))
        except (ValueError, TypeError):
            odds = 2.0

        if step == 1:
            if won:
                net_profit = BASE_STAKE * (odds - 1.0)
                locked_profit = round(net_profit / 2.0, 2)
                total_bank_profit += locked_profit
                next_stake = round(net_profit - locked_profit, 2)
                step = 2
            else:
                total_bank_profit -= BASE_STAKE
                next_stake = BASE_STAKE
                step = 1
        elif step == 2:
            if won:
                k2_payout = round(next_stake * odds, 2)
                next_stake = k2_payout
                step = 3
            else:
                # Prohra v K2 nebere uzamčený zisk z K1
                next_stake = BASE_STAKE
                step = 1
        elif step == 3:
            if won:
                k3_payout = round(next_stake * odds, 2)
                locked_k3 = round(k3_payout / 2.0, 2)
                total_bank_profit += locked_k3
                next_stake = round(k3_payout - locked_k3, 2)
                step = 4
            else:
                next_stake = BASE_STAKE
                step = 1
        elif step == 4:
            if won:
                k4_payout = round(next_stake * odds, 2)
                total_bank_profit += k4_payout
            next_stake = BASE_STAKE
            step = 1

    return step, int(round(next_stake)), round(total_bank_profit, 2)


def evaluate_pending_tickets():
    if not os.path.exists(HISTORY_FILE):
        print("Soubor tickets_history.json neexistuje.")
        return

    with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
        history = json.load(f)

    # 1. Nejprve aplikujeme případnou korekci kurzu z Telegramu (/kurz X.XX)
    history_changed = sync_telegram_odds_correction(history)

    pending = [t for t in history if t.get("status") == "pending"]
    if not pending:
        print("Všechny tikety jsou již vyhodnoceny (žádný status 'pending').")
        if history_changed:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        return

    print(f"Kontroluji {len(pending)} nevyhodnocených tiketů...")
    finished_matches = fetch_finished_matches()
    print(f"Staženo {len(finished_matches)} dohraných zápasů z API.")

    evaluated_reports = []

    for ticket in pending:
        items = ticket.get("ticket", [])
        if not items:
            continue

        all_found = True
        any_lost = False
        detail_lines = []

        for item in items:
            match_str = item.get("match", "")
            selection = item.get("selection", "")
            parts = re.split(r"\s+vs\.?\s+|\s+-\s+", match_str, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) != 2:
                all_found = False
                break

            home_q, away_q = parts[0].strip(), parts[1].strip()
            found_match = None

            for fm in finished_matches:
                if teams_match(home_q, fm["home"]) and teams_match(away_q, fm["away"]):
                    found_match = fm
                    break

            if not found_match:
                all_found = False
                detail_lines.append(f"⏳ {match_str}: zatím nedohráno")
                break

            res_bool = evaluate_selection(selection, found_match["home_goals"], found_match["away_goals"])
            score_str = f"{found_match['home_goals']}:{found_match['away_goals']}"

            if res_bool is True:
                detail_lines.append(f"✅ {match_str} ({score_str}) – `{selection}`")
            elif res_bool is False:
                any_lost = True
                detail_lines.append(f"❌ {match_str} ({score_str}) – `{selection}`")
            else:
                all_found = False
                detail_lines.append(f"⚠️ {match_str} ({score_str}) – nerozpoznaný tip `{selection}`")
                break

        if any_lost:
            ticket["status"] = "lost"
            ticket["evaluation_detail"] = " | ".join(detail_lines)
            history_changed = True
            evaluated_reports.append((ticket, False, detail_lines))
        elif all_found:
            ticket["status"] = "won"
            ticket["evaluation_detail"] = " | ".join(detail_lines)
            history_changed = True
            evaluated_reports.append((ticket, True, detail_lines))
        else:
            print(f"Tiket ID {ticket.get('id')} ještě čeká na dohrání všech zápasů.")

    if history_changed:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    if evaluated_reports:
        next_step, next_stake, bank_profit = calculate_progression_state(history)
        profit_sign = "+" if bank_profit >= 0 else ""

        for ticket, won, details in evaluated_reports:
            header = f"🏆 *TIKET ID {ticket.get('id')} VYHRÁL!*" if won else f"📉 *TIKET ID {ticket.get('id')} PROHRÁL*"
            lines = [
                header,
                f"📊 Kurz tiketu: `{ticket.get('total_odds')}`\n",
                *details,
                "\n━━━━━━━━━━━━━━━━━━━━",
                f"💰 *Aktuální čistý zisk banku:* `{profit_sign}{bank_profit:,.0f} Kč`",
                f"🎯 *Příští krok:* `Krok {next_step}/4`",
                f"💵 *Doporučený vklad na další tiket:* `{next_stake:,} Kč`"
            ]
            send_telegram_notification("\n".join(lines))
            print(f"Tiket ID {ticket.get('id')} vyhodnocen jako {'WON' if won else 'LOST'}.")


if __name__ == "__main__":
    evaluate_pending_tickets()

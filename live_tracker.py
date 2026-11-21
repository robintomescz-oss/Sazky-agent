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
    q_norm = normalize_team_name(query_team)
    a_norm = normalize_team_name(api_team)
    if not q_norm or not a_norm:
        return False
    if q_norm == a_norm or q_norm in a_norm or a_norm in q_norm:
        return True
    q_tokens = set(q_norm.split())
    a_tokens = set(a_norm.split())
    return bool(q_tokens & a_tokens and len(max(q_tokens & a_tokens, key=len)) >= 4)


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
    res = requests.post(url, json=payload, timeout=10)
    if res.status_code != 200:
        plain = message.replace("*", "").replace("_", "").replace("`", "")
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID.strip(), "text": plain}, timeout=10)


def _apply_double_aku_replacement(ticket, latest_odds):
    """Zavolá výměnu slabšího zápasu z agent.py. Na tiketu vždy zůstávají přesně 2 zápasy."""
    try:
        from agent import format_double_ticket, replace_weak_leg_for_double
    except Exception as e:
        print(f"Nepodařilo se načíst výměnu zápasu z agent.py: {e}")
        return False

    legs = (ticket.get("ticket") or [])[:2]
    if len(legs) != 2:
        return False

    try:
        leg1_odds = float(legs[0].get("real_odds", legs[0].get("odds", 0)) or 0)
        leg2_odds = float(legs[1].get("real_odds", legs[1].get("odds", 0)) or 0)
    except (TypeError, ValueError):
        return False

    # Telegram posílá jen celkový kurz. Pokud nesedí na součin uložených kurzů,
    # dopočítáme slabší nohu tak, aby odpovídala reálnému celkovému kurzu.
    stored_product = leg1_odds * leg2_odds
    if stored_product > 0 and abs(stored_product - latest_odds) > 0.05:
        kept_odds = max(leg1_odds, leg2_odds)
        weaker_odds = round(latest_odds / kept_odds, 2) if kept_odds > 0 else 0
        if weaker_odds > 1.0:
            if leg1_odds >= leg2_odds:
                leg2_odds = weaker_odds
            else:
                leg1_odds = weaker_odds

    updated = replace_weak_leg_for_double(ticket, leg1_odds, leg2_odds)
    if not updated or updated is ticket or updated.get("ticket") == ticket.get("ticket"):
        return False

    ticket["ticket"] = updated["ticket"][:2]
    ticket["total_odds"] = updated.get("total_odds", latest_odds)
    ticket["replacement_note"] = updated.get("replacement_note")
    ticket["analysis_summary"] = updated.get("analysis_summary", ticket.get("analysis_summary"))
    ticket["status"] = "pending"

    send_telegram(format_double_ticket(ticket))
    return True


def sync_telegram_odds_correction(history):
    """Zpracuje příkaz /kurz X.XX z Telegramu včetně výměny při kurzu pod 1.80."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    clean_token = TELEGRAM_BOT_TOKEN.replace("[", "").replace("]", "").replace("https://api.telegram.org/bot", "").strip()
    url = f"https://api.telegram.org/bot{clean_token}/getUpdates"

    try:
        res = requests.get(url, timeout=8)
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
                if old_odds == latest_odds:
                    return False

                # Kurz pod 1.80 -> vyměníme slabší zápas, ale tiket zůstává na 2 zápasech.
                if latest_odds < 1.80:
                    if _apply_double_aku_replacement(target, latest_odds):
                        print(f"Tiket ID {target.get('id')}: slabší zápas vyměněn, nový kurz {target.get('total_odds')}.")
                        return True

                    target["total_odds"] = latest_odds
                    send_telegram("\n".join([
                        "⚠️ *Korekce kurzu pod 1.80*",
                        f"Tiket ID {target.get('id')}: reálný kurz `{latest_odds}`.",
                        "Náhradní zápas se teď nepodařilo najít, tiket zůstává na 2 zápasech s upraveným kurzem."
                    ]))
                    return True

                target["total_odds"] = latest_odds
                send_telegram(
                    f"✏️ *Korekce kurzu potvrzena*\nTiket ID {target.get('id')}: celkový kurz upraven z `{old_odds}` na reálných `{latest_odds}`."
                )
                return True
    except Exception as e:
        print(f"Chyba Telegram korekce: {e}")
    return False


def fetch_live_and_recent_matches():
    """Stáhne aktuální živé, nadcházející i čerstvě dohrané zápasy z ESPN a football-data.org."""
    matches = []

    # 1. ESPN API (výborné pro live skóre, minuty zápasu i všechny soutěže)
    espn_leagues = [
        "eng.1", "eng.2", "esp.1", "ita.1", "ger.1", "fra.1",
        "ned.1", "por.1", "uefa.champions", "uefa.europa", "uefa.nations",
        "fifa.worldq.uefa", "fifa.friendly", "bra.1", "arg.1", "usa.1"
    ]
    dates_param = f"{(datetime.now() - timedelta(days=1)).strftime('%Y%m%d')}-{(datetime.now() + timedelta(days=1)).strftime('%Y%m%d')}"

    for slug in espn_leagues:
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={dates_param}"
        try:
            res = requests.get(url, timeout=6)
            if res.status_code != 200:
                continue
            for ev in res.json().get("events", []):
                status_obj = ev.get("status", {})
                state = status_obj.get("type", {}).get("state", "")  # 'pre', 'in', 'post'
                clock = status_obj.get("displayClock", "")
                detail = status_obj.get("type", {}).get("shortDetail", "")

                comps = ev.get("competitions", [])
                if not comps:
                    continue
                competitors = comps[0].get("competitors", [])
                if len(competitors) != 2:
                    continue

                home_comp = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away_comp = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

                try:
                    h_goals = int(home_comp.get("score", 0))
                    a_goals = int(away_comp.get("score", 0))
                except (ValueError, TypeError):
                    h_goals, a_goals = 0, 0

                matches.append({
                    "home": home_comp["team"]["displayName"],
                    "away": away_comp["team"]["displayName"],
                    "state": state,
                    "home_goals": h_goals,
                    "away_goals": a_goals,
                    "clock": clock or detail
                })
        except Exception:
            continue

    # 2. Záložní football-data.org (pokud zápas není na ESPN)
    if FOOTBALL_DATA_API_KEY:
        d_from = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        d_to = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        url = f"https://api.football-data.org/v4/matches?dateFrom={d_from}&dateTo={d_to}"
        headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY.strip()}
        try:
            res = requests.get(url, headers=headers, timeout=8)
            if res.status_code == 200:
                for m in res.json().get("matches", []):
                    st = m.get("status", "")
                    if st in ["IN_PLAY", "PAUSED", "HALFTIME"]:
                        state = "in"
                    elif st == "FINISHED":
                        state = "post"
                    else:
                        state = "pre"

                    ft = m.get("score", {}).get("fullTime", {})
                    h_g = int(ft["home"]) if ft.get("home") is not None else 0
                    a_g = int(ft["away"]) if ft.get("away") is not None else 0

                    matches.append({
                        "home": m["homeTeam"]["name"],
                        "away": m["awayTeam"]["name"],
                        "state": state,
                        "home_goals": h_g,
                        "away_goals": a_g,
                        "clock": "LIVE" if state == "in" else st
                    })
        except Exception:
            pass

    return matches


def evaluate_selection(selection, home_goals, away_goals):
    sel = selection.lower().strip()
    total_goals = home_goals + away_goals

    if "1x" in sel or "neprohra domácích" in sel or "neprohra domacich" in sel:
        return home_goals >= away_goals
    if "x2" in sel or "neprohra hostů" in sel or "neprohra hostu" in sel:
        return away_goals >= home_goals
    if "výhra domácích" in sel or "vyhra domacich" in sel or sel in {"1", "home"} or "(1)" in sel:
        return home_goals > away_goals
    if "výhra hostů" in sel or "vyhra hostu" in sel or sel in {"2", "away"} or "(2)" in sel:
        return away_goals > home_goals
    if "remíza" in sel or "remiza" in sel or sel in {"x", "draw"}:
        return home_goals == away_goals

    over_match = re.search(r"over\s*([0-9]+(?:\.[0-9]+)?)", sel)
    if over_match:
        return total_goals > float(over_match.group(1))

    under_match = re.search(r"under\s*([0-9]+(?:\.[0-9]+)?)", sel)
    if under_match:
        return total_goals < float(under_match.group(1))

    if "btts" in sel or "oba dají gól" in sel:
        return home_goals > 0 and away_goals > 0

    return None


def calculate_progression_state(history):
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


def run_live_tracker():
    if not os.path.exists(HISTORY_FILE):
        print("Historie neexistuje, končím.")
        return

    with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
        history = json.load(f)

    changed = sync_telegram_odds_correction(history)
    pending_tickets = [t for t in history if t.get("status") == "pending"]

    if not pending_tickets:
        print("Žádný aktivní (pending) tiket ke sledování.")
        if changed:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        return

    live_data = fetch_live_and_recent_matches()
    print(f"Načteno {len(live_data)} zápasů z live feedu.")

    for ticket in pending_tickets:
        items = ticket.get("ticket", [])
        all_finished = True
        any_lost = False
        summary_lines = []

        for item in items:
            match_str = item.get("match", "")
            selection = item.get("selection", "")
            parts = re.split(r"\s+vs\.?\s+|\s+-\s+", match_str, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) != 2:
                all_finished = False
                continue

            home_q, away_q = parts[0].strip(), parts[1].strip()
            found = next((m for m in live_data if teams_match(home_q, m["home"]) and teams_match(away_q, m["away"])), None)

            if not found:
                all_finished = False
                continue

            prev_state = item.get("live_state", "pre")
            prev_h = item.get("live_home", 0)
            prev_a = item.get("live_away", 0)

            curr_state = found["state"]
            curr_h = found["home_goals"]
            curr_a = found["away_goals"]
            clock = found.get("clock", "")

            # 1. Hlášení začátku zápasu
            if prev_state == "pre" and curr_state in ["in", "post"]:
                send_telegram(
                    f"🟢 *ZÁPAS ZAČAL*\n⚽ *{match_str}*\n🎯 Tvůj tip: `{selection}` (kurz `{item.get('odds')}`)"
                )
                item["live_state"] = curr_state
                changed = True

            # 2. Hlášení gólu (změna skóre během zápasu nebo při dohrání)
            if curr_state in ["in", "post"] and (curr_h != prev_h or curr_a != prev_a):
                if curr_h > prev_h:
                    scorer_msg = f"Gól domácích ({home_q})!"
                elif curr_a > prev_a:
                    scorer_msg = f"Gól hostů ({away_q})!"
                else:
                    scorer_msg = "Úprava skóre (VAR):"

                tip_ok = evaluate_selection(selection, curr_h, curr_a)
                status_icon = "✅ Průběžně vychází" if tip_ok else "⚠️ Průběžně nevychází"

                send_telegram(
                    f"⚽ *GÓL! {scorer_msg}*\n*{home_q} {curr_h}:{curr_a} {away_q}* ({clock})\n🎯 Tip `{selection}`: {status_icon}"
                )
                item["live_home"] = curr_h
                item["live_away"] = curr_a
                changed = True

            # 3. Hlášení konce jednotlivého zápasu
            if prev_state != "post" and curr_state == "post":
                tip_won = evaluate_selection(selection, curr_h, curr_a)
                res_badge = "✅ TIP VYŠEL" if tip_won else "❌ TIP NEVYŠEL"
                send_telegram(
                    f"🏁 *KONEC ZÁPASU*\n*{home_q} {curr_h}:{curr_a} {away_q}*\n🎯 Tip `{selection}`: *{res_badge}*"
                )
                item["live_state"] = "post"
                item["live_home"] = curr_h
                item["live_away"] = curr_a
                changed = True

            # Kontrola stavu pro celkové uzavření tiketu
            if curr_state != "post":
                all_finished = False
            else:
                tip_won = evaluate_selection(selection, curr_h, curr_a)
                if tip_won is False:
                    any_lost = True
                    summary_lines.append(f"❌ {match_str} ({curr_h}:{curr_a}) – `{selection}`")
                elif tip_won is True:
                    summary_lines.append(f"✅ {match_str} ({curr_h}:{curr_a}) – `{selection}`")
                else:
                    all_finished = False

        # 4. Finální uzavření celého tiketu, pokud už oba zápasy skončily
        if all_finished and items:
            ticket_won = not any_lost
            ticket["status"] = "won" if ticket_won else "lost"
            changed = True

            next_step, next_stake, bank_profit = calculate_progression_state(history)
            profit_sign = "+" if bank_profit >= 0 else ""
            header = f"🏆 *SOUHRN TIKETU ID {ticket.get('id')}: VÝHRA!*" if ticket_won else f"📉 *SOUHRN TIKETU ID {ticket.get('id')}: PROHRA*"

            report = [
                header,
                f"📊 Celkový kurz: `{ticket.get('total_odds')}`\n",
                *summary_lines,
                "\n━━━━━━━━━━━━━━━━━━━━",
                f"💰 *Čistý zisk banku:* `{profit_sign}{bank_profit:,.0f} Kč`",
                f"🎯 *Další krok:* `Krok {next_step}/4`",
                f"💵 *Doporučený vklad:* `{next_stake:,} Kč`"
            ]
            send_telegram("\n".join(report))

    if changed:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        print("Změny uloženy do tickets_history.json.")


if __name__ == "__main__":
    run_live_tracker()

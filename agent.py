import os
import json
import re
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv

try:
    from json_repair import repair_json
except ImportError:
    repair_json = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

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
) if OPENROUTER_API_KEY and OpenAI else None


def parse_and_repair_json(text):
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        raw_json_str = match.group(0) if match else text
        repaired = repair_json(raw_json_str, return_objects=True) if repair_json else json.loads(raw_json_str)
        if isinstance(repaired, dict):
            return repaired
    except Exception as e:
        print(f"Chyba při opravě JSON: {e}")
    raise ValueError("Nepodařilo se extrahovat platný JSON objekt.")


def normalize_match_name(name):
    if not name:
        return ""
    text = re.sub(r"[^a-z0-9]+", " ", str(name).lower())
    return " ".join(text.split())


def same_match(home_a, away_a, home_b, away_b):
    norm_a = f"{normalize_match_name(home_a)} {normalize_match_name(away_a)}".strip()
    norm_b = f"{normalize_match_name(home_b)} {normalize_match_name(away_b)}".strip()
    if not norm_a or not norm_b:
        return False
    shared = set(norm_a.split()) & set(norm_b.split())
    return len(shared) >= 2 and len(max(shared, key=len)) >= 3


def fetch_odds_candidates(min_odds=1.35, max_odds=1.72, hours=5, exclude_matches=None):
    if not ODDS_API_KEY:
        return []
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds"
    params = {"apiKey": ODDS_API_KEY.strip(), "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
    excluded = exclude_matches or []
    try:
        res = requests.get(url, params=params, timeout=15)
        if res.status_code != 200:
            print(f"The Odds API returned {res.status_code}: {res.text}")
            return []
        print(f"The Odds API OK (remaining credits: {res.headers.get('x-requests-remaining', 'unknown')}).")
        now = datetime.now(timezone.utc)
        end = now + timedelta(hours=min(max(int(hours), 1), 5))
        candidates = []
        for event in res.json():
            key = str(event.get("sport_key", "")).lower()
            title = str(event.get("sport_title", key))
            title_lower = title.lower()
            if key.startswith("soccer_"):
                sport = "soccer"
                band_min = 1.38
            elif key.startswith("tennis_"):
                sport = "tennis"
                band_min = 1.35
                if re.search(r"\bitf\b|challenger|futures", key + " " + title_lower):
                    continue
                if not re.search(r"\batp\b|\bwta\b|grand slam|australian open|roland garros|french open|wimbledon|us open", key + " " + title_lower):
                    continue
            elif key.startswith("basketball_"):
                sport = "basketball"
                band_min = 1.35
                if not re.search(r"\bnba\b|euroleague", key + " " + title_lower):
                    continue
            else:
                continue
            try:
                commence = datetime.fromisoformat(str(event.get("commence_time", "")).replace("Z", "+00:00"))
                if commence < now + timedelta(minutes=5) or commence > end:
                    continue
            except Exception:
                continue
            home, away = event.get("home_team", ""), event.get("away_team", "")
            if not home or not away or any(same_match(home, away, x, y) for x, y in excluded):
                continue
            totals = {}
            counts = {}
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    for outcome in market.get("outcomes", []):
                        try:
                            price = float(outcome.get("price"))
                        except (TypeError, ValueError):
                            continue
                        if price > 1:
                            name = outcome.get("name", "")
                            totals[name] = totals.get(name, 0.0) + price
                            counts[name] = counts.get(name, 0) + 1
            averages = {name: total / counts[name] for name, total in totals.items() if counts[name]}
            implied_raw = {name: 1.0 / price for name, price in averages.items()}
            margin = sum(implied_raw.values())
            if margin <= 0:
                continue
            probs = {name: p / margin for name, p in implied_raw.items()}
            options = []
            if sport == "soccer":
                home_odds = averages.get(home, 0)
                draw_odds = averages.get("Draw", 0)
                away_odds = averages.get(away, 0)
                if home_odds > 1:
                    options.append(("Výhra domácích (1)", home_odds, probs.get(home, 0)))
                if home_odds > 1 and draw_odds > 1:
                    odds_1x = 1.0 / ((1.0 / home_odds) + (1.0 / draw_odds))
                    if 1.38 <= odds_1x <= 1.72:
                        options.append(("Neprohra domácích (1X)", odds_1x, probs.get(home, 0) + probs.get("Draw", 0)))
            else:
                for team in (home, away):
                    price = averages.get(team, 0)
                    if price > 1:
                        options.append(("Výhra domácích" if team == home else "Výhra hostů", price, probs.get(team, 0)))
            valid = [x for x in options if max(min_odds, band_min) <= x[1] <= min(max_odds, 1.72)]
            if not valid:
                continue
            selection, odds, probability = max(valid, key=lambda x: x[2])
            candidates.append({
                "home": home, "away": away, "match": f"{home} vs {away}", "league": title,
                "sport": sport, "sport_key": key, "commence_time": event.get("commence_time", ""),
                "recommended_selection": selection, "real_odds": round(odds, 2),
                "probability": probability, "confidence_reason": f"Tržní odhad pravděpodobnosti {probability:.1%} z průměrných EU kurzů."
            })
        return candidates
    except Exception as e:
        print(f"The Odds API request failed: {e}")
        return []

def fetch_real_odds_matches():
    candidates = fetch_odds_candidates(1.35, 1.72, hours=5)
    candidates.sort(key=lambda item: float(item.get("probability", 0)), reverse=True)
    print(f"The Odds API: nalezeno {len(candidates)} fotbalových, tenisových a basketbalových tipů do 5 hodin.")
    return candidates[:40]


def _leg_real_odds(leg):
    try:
        return float(leg.get("real_odds", leg.get("odds", 0)))
    except (TypeError, ValueError):
        return 0.0


def _leg_started(leg):
    return str(leg.get("live_state", "pre")).lower() in ("in", "post")


def _ticket_leg_exclusions(legs):
    exclusions = []
    for leg in legs:
        match_str = leg.get("match", "")
        parts = re.split(r"\s+vs\.?\s+|\s+-\s+", match_str, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            exclusions.append((parts[0].strip(), parts[1].strip()))
        elif leg.get("home") and leg.get("away"):
            exclusions.append((leg["home"], leg["away"]))
    return exclusions


def _make_leg(candidate):
    odds = round(float(candidate.get("real_odds", 0)), 2)
    return {
        "match": candidate.get("match", f"{candidate.get('home', '')} vs {candidate.get('away', '')}"),
        "league": candidate.get("league", "Fotbal"),
        "selection": candidate.get("recommended_selection", "Výhra domácích (1)"),
        "odds": odds,
        "real_odds": odds,
        "confidence_reason": candidate.get("confidence_reason", "Favorit podle tržní pravděpodobnosti."),
        "probability": candidate.get("probability", 0),
        "sport": candidate.get("sport", "soccer"),
        "sport_key": candidate.get("sport_key", ""),
        "commence_time": candidate.get("commence_time", "")
    }


def build_new_double_pair():
    
    pool = [
        c for c in fetch_odds_candidates(1.35, 1.72, hours=5)
        if 1.35 <= float(c.get("real_odds", 0)) <= 1.72
    ]
    if len(pool) < 2:
        return None

    best = None
    best_rank = None
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if same_match(pool[i]["home"], pool[i]["away"], pool[j]["home"], pool[j]["away"]):
                continue
            product = float(pool[i]["real_odds"]) * float(pool[j]["real_odds"])
            probability = float(pool[i].get("probability", 0)) * float(pool[j].get("probability", 0))
            in_target = 2.05 <= product <= 2.45
            rank = (1 if in_target else 0, probability, -abs(product - 2.20))
            if best_rank is None or rank > best_rank:
                best_rank, best = rank, (pool[i], pool[j])

    if not best:
        return None

    legs = [_make_leg(best[0]), _make_leg(best[1])]
    return {
        "ticket": legs,
        "total_odds": round(legs[0]["real_odds"] * legs[1]["real_odds"], 2),
        "analysis_summary": "Celá dvojice byla přestavěna, protože oba původní zápasy měly kurz pod 1.30.",
        "replacement_note": "Vygenerována zcela nová dvojice 2 zápasů (pásma 1.35–1.72, součin ~2.20)."
    }


def replace_weak_leg_for_double(current_ticket, leg1_odds, leg2_odds=None):
    
    if not current_ticket or not isinstance(current_ticket.get("ticket"), list):
        return current_ticket

    legs = [dict(leg) for leg in current_ticket.get("ticket", [])[:2]]
    if len(legs) != 2:
        return current_ticket

    try:
        odds1 = float(leg1_odds)
    except (TypeError, ValueError):
        odds1 = _leg_real_odds(legs[0])
    try:
        odds2 = float(leg2_odds) if leg2_odds is not None else _leg_real_odds(legs[1])
    except (TypeError, ValueError):
        odds2 = _leg_real_odds(legs[1])

    if odds1 <= 1.0 or odds2 <= 1.0:
        return current_ticket

    if odds1 * odds2 >= 1.80:
        return current_ticket

    # Oba zápasy mají příliš nízký kurz -> celá nová dvojice (opět přesně 2 zápasy)
    if odds1 < 1.30 and odds2 < 1.30:
        new_pair = build_new_double_pair()
        if not new_pair:
            print("Pro novou dvojici není v nabídce dostatek zápasů v pásmu 1.48–1.72.")
            return current_ticket
        result = dict(current_ticket)
        result.update(new_pair)
        result["ticket"] = result["ticket"][:2]
        return result

    # Slabší zápas = přednostně ten, který ještě nezačal, jinak ten s nižším kurzem
    not_started = [i for i in (0, 1) if not _leg_started(legs[i])]
    if not not_started:
        print("Oba zápasy už začaly, výměnu nelze provést.")
        return current_ticket

    if len(not_started) == 1:
        weaker_idx = not_started[0]
    else:
        weaker_idx = 0 if odds1 <= odds2 else 1

    kept_idx = 1 - weaker_idx
    kept_odds = odds1 if kept_idx == 0 else odds2
    target_odds = round(2.20 / kept_odds, 2)

    excluded = _ticket_leg_exclusions(legs)
    candidates = fetch_odds_candidates(1.35, 1.72, hours=5, exclude_matches=excluded)
    if not candidates:
        print("The Odds API nevrátilo žádného vhodného náhradníka v pásmu 1.42–1.78.")
        return current_ticket

    candidates.sort(key=lambda c: (
        0 if abs(float(c["real_odds"]) * kept_odds - 2.20) <= 0.25 else 1,
        -float(c.get("probability", 0)),
        abs(float(c["real_odds"]) - target_odds)
    ))
    best = candidates[0]

    legs[kept_idx]["odds"] = round(kept_odds, 2)
    legs[kept_idx]["real_odds"] = round(kept_odds, 2)

    replaced_leg = _make_leg(best)
    replaced_leg["replaced_match"] = legs[weaker_idx].get("match", "")
    legs[weaker_idx] = replaced_leg

    result = dict(current_ticket)
    result["ticket"] = legs[:2]
    result["total_odds"] = round(kept_odds * float(replaced_leg["real_odds"]), 2)
    result["replacement_note"] = (
        f"Slabší zápas nahrazen: {replaced_leg['match']} (kurz {replaced_leg['real_odds']}), "
        f"cílový kurz náhrady byl {target_odds}."
    )
    return result


def format_double_ticket(ticket_data, title="🎯 *Aktualizovaný Double AKU tiket*"):
    
    step, stake, step_desc = get_current_stake_info()
    legs = (ticket_data or {}).get("ticket", [])[:2]
    lines = [title, ""]
    for idx, leg in enumerate(legs, 1):
        lines.append(f"{idx}. ⚽ *{leg.get('match', '')}* ({leg.get('league', '')})")
        lines.append(f"   Tip: `{leg.get('selection', '')}` | Kurz: `{leg.get('real_odds', leg.get('odds', ''))}`")
    lines.append("")
    lines.append(f"📊 *Celkový kurz:* `{ticket_data.get('total_odds', 'N/A')}`")
    lines.append(f"💰 *Staking:* *{step_desc}* (vklad {stake} Kč)")
    if ticket_data.get("replacement_note"):
        lines.append(f"🔄 _{ticket_data['replacement_note']}_")
    if ticket_data.get("analysis_summary"):
        lines.append(f"💡 {ticket_data['analysis_summary']}")
    lines.append("")
    lines.append("✅ *Na tiketu jsou vždy přesně 2 zápasy.*")
    return "\n".join(lines)


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
        return 1, int(base_stake), "Krok 1/4 (Základní vklad 1 000 Kč)"

    base_stake = next((float(t.get("base_stake")) for t in reversed(history) if float(t.get("base_stake", 0) or 0) > 0), BASE_STAKE)
    evaluated = [t for t in history if t.get("status") in ["won", "lost"]]
    step = 1
    next_stake = base_stake
    k2_payout = 0.0

    for t in evaluated:
        won = (t["status"] == "won")
        try:
            odds = float(t.get("total_odds", 2.0))
        except (ValueError, TypeError):
            odds = 2.0

        if step == 1:
            if won:
                net_profit = base_stake * (odds - 1.0)
                next_stake = round(net_profit / 2.0, 2)
                step = 2
            else:
                next_stake = base_stake
                step = 1
        elif step == 2:
            if won:
                k2_payout = round(next_stake * odds, 2)
                next_stake = k2_payout
                step = 3
            else:
                next_stake = base_stake
                step = 1
        elif step == 3:
            if won:
                next_stake = round(k2_payout / 2.0, 2)
                step = 4
            else:
                next_stake = base_stake
                step = 1
        elif step == 4:
            next_stake = base_stake
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
    if not has_real_odds or not matches:
        print("No verified Odds API picks are available; refusing to invent prices.")
        return None
    pool = [m for m in matches if float(m.get("real_odds", 0) or 0) > 1 and float(m.get("probability", 0) or 0) > 0]
    pool.sort(key=lambda item: float(item.get("probability", 0)), reverse=True)
    best = None
    best_rank = None
    for index, first in enumerate(pool):
        for second in pool[index + 1:]:
            if same_match(first.get("home", ""), first.get("away", ""), second.get("home", ""), second.get("away", "")):
                continue
            product = float(first["real_odds"]) * float(second["real_odds"])
            probability = float(first["probability"]) * float(second["probability"])
            in_band = 2.05 <= product <= 2.45
            rank = (1 if in_band else 0, probability, -abs(product - 2.20))
            if best_rank is None or rank > best_rank:
                best_rank, best = rank, (first, second)
    if not best:
        return None
    legs = [_make_leg(best[0]), _make_leg(best[1])]
    return {
        "ticket": legs,
        "total_odds": round(legs[0]["real_odds"] * legs[1]["real_odds"], 2),
        "analysis_summary": "Dvojice dvou různých událostí s nejvyšší kombinovanou tržní pravděpodobností v pásmu cílového kurzu."
    }

def save_ticket_to_history(ticket_data):
    if not ticket_data or not ticket_data.get("ticket"):
        return

    # STRIKTNÍ PRAVIDLO DOUBLE AKU: uloží se vždy právě 2 zápasy.
    legs = ticket_data.get("ticket", [])[:2]
    if len(legs) != 2:
        print("Tiket nemá přesně 2 zápasy, neukládám.")
        return

    try:
        total_odds = round(_leg_real_odds(legs[0]) * _leg_real_odds(legs[1]), 2)
    except Exception:
        total_odds = ticket_data.get("total_odds")

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
        "total_odds": total_odds,
        "status": "pending",
        "ticket": legs,
        "summary": ticket_data.get("analysis_summary")
    }
    entry["base_stake"] = next((float(t.get("base_stake")) for t in reversed(history) if float(t.get("base_stake", 0) or 0) > 0), BASE_STAKE)
    history.append(entry)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def send_telegram(ticket_data, has_real_odds):
    step, stake, step_desc = get_current_stake_info()

    if not ticket_data or not ticket_data.get("ticket"):
        message = "ℹ️ V aktuální nabídce teď nejsou žádné zápasy splňující bezpečná kurzová kritéria (1.42–1.65)."
    else:
        source_badge = "Fotbal / tenis / basket · EU kurzy (cíl ~2.20)" if has_real_odds else "Orientační kurzy (cíl ~2.20)"
        lines = [f"🎯 *Sportovní Double AKU tiket*\n_{source_badge}_\n"]
        for idx, item in enumerate(ticket_data["ticket"], 1):
            lines.append(f"{idx}. ⚽ *{item.get('match', '')}* ({item.get('league', '')})")
            lines.append(f"   Tip: `{item.get('selection', '')}` | Kurz: `{item.get('odds', '')}`")
            lines.append(f"   Důvod: _{item.get('confidence_reason', '')}_\n")

        lines.append(f"📊 *Celkový kurz (EU):* `{ticket_data.get('total_odds', 'N/A')}`")
        lines.append(f"💰 *Staking:* *{step_desc}*")
        lines.append(f"💡 *Komentář:* {ticket_data.get('analysis_summary', '')}")
        lines.append("\n✏️ _Pokud na Tipsportu vsadíš jiný celkový kurz, odpověz botovi zprávou ve tvaru:_ `/kurz 2.08`")
        message = "\n".join(lines)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("CHYBA: Chybí TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID v environment proměnných!")
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
        print(f"Telegram Markdown selhal ({res.status_code}: {res.text}), odesílám jako čistý text...")
        plain_message = message.replace("*", "").replace("_", "").replace("`", "")
        payload_plain = {
            "chat_id": TELEGRAM_CHAT_ID.strip(),
            "text": plain_message
        }
        res2 = requests.post(url, json=payload_plain, timeout=10)
        print(f"Výsledek záložního odeslání: {res2.status_code}")
    else:
        print("Zpráva úspěšně doručena na Telegram.")


if __name__ == "__main__":
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
                existing = json.load(f)
            if any(item.get("status") == "pending" for item in existing):
                print("Existuje pending tiket; nový tiket se nevytvoří.")
                raise SystemExit(0)
        except (json.JSONDecodeError, OSError):
            print("Historie je nečitelná; nový tiket se nevytvoří kvůli bezpečnosti.")
            raise SystemExit(1)
    print("1/4 Stahuji události a tržní kurzy (max. 5 hodin)...")
    matches, has_real_odds = fetch_matches()
    print(f"Celkem připraveno {len(matches)} kandidátů (ověřené kurzy: {has_real_odds}).")
    ticket = generate_ticket(matches, has_real_odds)
    if not ticket:
        send_telegram(None, has_real_odds)
        raise SystemExit(0)
    save_ticket_to_history(ticket)
    send_telegram(ticket, has_real_odds)
    print("Hotovo!")

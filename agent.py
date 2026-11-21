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


def fetch_odds_candidates(min_odds=1.42, max_odds=1.65, hours=48, exclude_matches=None):
    """Stáhne fotbalovou nabídku z The Odds API a vrátí tipy (1 nebo 1X) v kurzovém pásmu."""
    if not ODDS_API_KEY:
        return []

    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds"
    params = {
        "apiKey": ODDS_API_KEY.strip(),
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal"
    }
    excluded = exclude_matches or []

    try:
        res = requests.get(url, params=params, timeout=12)
        if res.status_code != 200:
            print(f"The Odds API vrátilo status {res.status_code}: {res.text}")
            return []

        remaining = res.headers.get("x-requests-remaining", "neznámo")
        print(f"The Odds API OK (zbývající kredity: {remaining}).")

        events = res.json()
        now_utc = datetime.now(timezone.utc)
        limit_utc = now_utc + timedelta(hours=hours)
        candidates = []

        for ev in events:
            sport_key = ev.get("sport_key", "")
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

            if any(same_match(home, away, ex_home, ex_away) for ex_home, ex_away in excluded):
                continue

            bookmakers = ev.get("bookmakers", [])
            if not bookmakers:
                continue

            home_odds_list = []
            draw_odds_list = []
            away_odds_list = []

            for bm in bookmakers:
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != "h2h":
                        continue
                    outcomes = {o.get("name"): o.get("price") for o in mkt.get("outcomes", [])}
                    try:
                        home_price = float(outcomes[home])
                        draw_price = float(outcomes["Draw"])
                        away_price = float(outcomes[away])
                    except (KeyError, TypeError, ValueError):
                        continue
                    home_odds_list.append(home_price)
                    away_odds_list.append(away_price)
                    draw_odds_list.append(draw_price)

            if not home_odds_list:
                continue

            avg_home = round(sum(home_odds_list) / len(home_odds_list), 2)
            avg_draw = round(sum(draw_odds_list) / len(draw_odds_list), 2)
            avg_away = round(sum(away_odds_list) / len(away_odds_list), 2)
            double_chance_1x = round(1.0 / ((1.0 / avg_home) + (1.0 / avg_draw)), 2)

            base = {
                "home": home,
                "away": away,
                "match": f"{home} vs {away}",
                "league": league,
                "utcDate": commence_str[:16].replace("T", " "),
                "commence_time": commence_str,
                "sport_key": sport_key,
                "market_context": f"1: {avg_home} | X: {avg_draw} | 2: {avg_away} | 1X: {double_chance_1x}"
            }

            if min_odds <= avg_home <= max_odds:
                candidates.append(dict(
                    base,
                    recommended_selection="Výhra domácích (1)",
                    real_odds=avg_home
                ))
            elif min_odds <= double_chance_1x <= max_odds:
                candidates.append(dict(
                    base,
                    recommended_selection="Neprohra domácích (1X)",
                    real_odds=double_chance_1x
                ))

        return candidates

    except Exception as e:
        print(f"Chyba při komunikaci s The Odds API: {e}")
        return []


def fetch_real_odds_matches():
    candidates = fetch_odds_candidates(1.42, 1.65)
    print(f"The Odds API: nalezeno {len(candidates)} zápasů v pásmu 1.42–1.65.")
    return candidates[:20]


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
        "confidence_reason": candidate.get("confidence_reason", "Bezpečný favorit v kurzovém pásmu."),
        "commence_time": candidate.get("commence_time", "")
    }


def build_new_double_pair():
    """Sestaví zcela novou dvojici 2 zápasů v pásmu 1.48–1.72 se součinem kolem 2.15."""
    pool = [
        c for c in fetch_odds_candidates(1.48, 1.72)
        if 1.48 <= float(c.get("real_odds", 0)) <= 1.72
    ]
    if len(pool) < 2:
        return None

    best = None
    best_diff = None
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if same_match(pool[i]["home"], pool[i]["away"], pool[j]["home"], pool[j]["away"]):
                continue
            product = float(pool[i]["real_odds"]) * float(pool[j]["real_odds"])
            diff = abs(product - 2.15)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best = (pool[i], pool[j])

    if not best:
        return None

    legs = [_make_leg(best[0]), _make_leg(best[1])]
    return {
        "ticket": legs,
        "total_odds": round(legs[0]["real_odds"] * legs[1]["real_odds"], 2),
        "analysis_summary": "Celá dvojice byla přestavěna, protože oba původní zápasy měly kurz pod 1.30.",
        "replacement_note": "Vygenerována zcela nová dvojice 2 zápasů (pásmo 1.48–1.72, součin ~2.15)."
    }


def replace_weak_leg_for_double(current_ticket, leg1_odds, leg2_odds=None):
    """Vymění slabší ze DVOU zápasů tak, aby součin vyšel kolem 2.15. Nikdy nepřidá třetí zápas."""
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
    target_odds = round(2.15 / kept_odds, 2)

    excluded = _ticket_leg_exclusions(legs)
    candidates = fetch_odds_candidates(1.42, 1.78, exclude_matches=excluded)
    if not candidates:
        print("The Odds API nevrátilo žádného vhodného náhradníka v pásmu 1.42–1.78.")
        return current_ticket

    best = min(
        candidates,
        key=lambda c: (abs(float(c["real_odds"]) - target_odds), float(c["real_odds"]))
    )

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
    """Naformátuje tiket tak, aby měl vždy právě 2 zápasy."""
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
    matches_json = json.dumps(matches, ensure_ascii=False, indent=2)

    if has_real_odds:
        prompt_lines = [
            f"Dnešní datum je: {today}.",
            "Zde je seznam nadcházejících zápasů s REÁLNÝMI EVROPSKÝMI KURZY (pásmo 1.42–1.65):",
            matches_json,
            "Vyber PŘÍSNĚ 2 NEJBEZPEČNĚJŠÍ ZÁPASY do Double AKU tiketu:",
            "- Použij PŘESNĚ hodnotu 'recommended_selection' a PŘESNÉ číslo 'real_odds' uvedené u vybraných zápasů.",
            "- Celkový kurz ('total_odds') musí být přesný součin obou kurzů s cílem kolem 2.20 (rozmezí 2.05 až 2.45).",
            "- Preferuj známější evropské soutěže a favority s jasnou domácí převahou.",
            'Vrať VÝHRADNĚ platný JSON v této struktuře: {"ticket": [{"match": "Domácí vs Hosté", "league": "Název ligy", "selection": "Výhra domácích (1)", "odds": 1.48, "confidence_reason": "stručný důvod"}, {"match": "Domácí vs Hosté", "league": "Název ligy", "selection": "Neprohra domácích (1X)", "odds": 1.49, "confidence_reason": "stručný důvod"}], "total_odds": 2.21, "analysis_summary": "krátké zhodnocení"}'
        ]
    else:
        prompt_lines = [
            f"Dnešní datum je: {today}.",
            "Zde je seznam dostupných zápasů (záložní rozpis bez kurzů):",
            matches_json,
            "Sestav AKU tiket PŘÍSNĚ ZE 2 NEJBEZPEČNĚJŠÍCH ZÁPASŮ (jednotlivý kurz 1.45–1.55, cílový celkový kurz kolem 2.20).",
            'Vrať VÝHRADNĚ platný JSON v této struktuře: {"ticket": [{"match": "Domácí vs Hosté", "league": "Název ligy", "selection": "Výhra domácích", "odds": 1.48, "confidence_reason": "stručný důvod"}, {"match": "Domácí vs Hosté", "league": "Název ligy", "selection": "Over 1.5", "odds": 1.48, "confidence_reason": "stručný důvod"}], "total_odds": 2.19, "analysis_summary": "krátké zhodnocení"}'
        ]

    user_prompt = "\n".join(prompt_lines)

    print("Zkouším primární motor: Google Gemini 2.5 Flash...")
    ticket_data = ask_gemini(system_prompt, user_prompt)

    if not ticket_data:
        print("Přepínám na záložní motor: OpenRouter...")
        ticket_data = ask_openrouter(system_prompt, user_prompt)

    if ticket_data and isinstance(ticket_data.get("ticket"), list):
        # STRIKTNÍ PRAVIDLO DOUBLE AKU: tiket má vždy právě 2 zápasy, nikdy 3.
        ticket_data["ticket"] = ticket_data["ticket"][:2]
        if len(ticket_data["ticket"]) != 2:
            print("Model nevrátil přesně 2 zápasy, tiket se neukládá.")
            return None
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
    history.append(entry)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def send_telegram(ticket_data, has_real_odds):
    step, stake, step_desc = get_current_stake_info()

    if not ticket_data or not ticket_data.get("ticket"):
        message = "ℹ️ V aktuální nabídce teď nejsou žádné zápasy splňující bezpečná kurzová kritéria (1.42–1.65)."
    else:
        source_badge = "Reálné EU kurzy (Cíl ~2.20)" if has_real_odds else "Orientační kurzy (Cíl ~2.20)"
        lines = [f"🎯 *Fotbalový Double AKU tiket*\n_{source_badge}_\n"]
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
    print("1/4 Stahuji zápasy a reálné kurzy (min. 1.42)...")
    matches, has_real_odds = fetch_matches()
    print(f"Celkem připraveno {len(matches)} kandidátů (reálné kurzy: {has_real_odds}).")

    print("2/4 Analyzuji nabídku a sestavuji tiket...")
    ticket = generate_ticket(matches, has_real_odds)

    print("3/4 Ukládám do historie...")
    save_ticket_to_history(ticket)

    print("4/4 Odesílám na Telegram...")
    send_telegram(ticket, has_real_odds)
    print("Hotovo!")

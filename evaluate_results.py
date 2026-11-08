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
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("V odpovědi nebyl nalezen validní JSON.")

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def evaluate_pending_tickets(pending_tickets):
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""
Jsi sportovní analytik a kontrolor fotbalových výsledků.
Dnešní datum je: {today}.

Zde je seznam tiketů čekajících na vyhodnocení:
{json.dumps(pending_tickets, ensure_ascii=False, indent=2)}

Tvým úkolem je:
1. Zkontrolovat výsledky zápasů pro jednotlivé tipy.
2. Pokud zápas ještě neskončil nebo výsledek není znám, status zůstává "pending".
3. Pokud všechny tipy na tiketu vyšly, status tiketu je "won".
4. Pokud alespoň jeden tip nevyšel, status tiketu je "lost".

Vrať VÝHRADNĚ validní JSON v tomto formátu:
{{
  "evaluations": [
    {{
      "id": 1,
      "status": "won",
      "result_detail": "Arsenal vyhrál 2:0 (OK), Real vyhrál 3:1 (OK)"
    }}
  ]
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

    eval_data = None
    for model_name in free_models:
        try:
            print(f"Vyhodnocuji přes model: {model_name}...")
            res = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "Jsi přesný sportovní vyhodnocovač. Odpovídej výhradně ve validním JSON formátu."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1200,
                temperature=0.1
            )
            if res and res.choices and res.choices[0].message.content:
                eval_data = extract_json(res.choices[0].message.content.strip())
                print(f"Úspěšně vyhodnoceno modelem: {model_name}")
                break
        except Exception as e:
            print(f"Model {model_name} selhal ({e}), zkouším další...")
            continue

    return eval_data

def calculate_stats(history):
    total = len(history)
    won = sum(1 for t in history if t.get("status") == "won")
    lost = sum(1 for t in history if t.get("status") == "lost")
    pending = sum(1 for t in history if t.get("status") == "pending")

    # Výpočet zisku při jednotkové sázce 1 unit na každý tiket
    profit = 0.0
    for t in history:
        if t.get("status") == "won":
            odds = float(t.get("total_odds", 2.0))
            profit += (odds - 1.0)
        elif t.get("status") == "lost":
            profit -= 1.0

    completed = won + lost
    win_rate = (won / completed * 100) if completed > 0 else 0.0
    roi = (profit / completed * 100) if completed > 0 else 0.0

    return {
        "total": total,
        "won": won,
        "lost": lost,
        "pending": pending,
        "profit": round(profit, 2),
        "win_rate": round(win_rate, 1),
        "roi": round(roi, 1)
    }

def send_telegram(evaluated_summary, stats):
    lines = ["🌙 *Večerní vyhodnocení fotbalových tiketů*\n"]

    if evaluated_summary:
        lines.append("*Vyhodnocené tikety:*")
        for item in evaluated_summary:
            status_emoji = "✅ VÝHRA" if item["status"] == "won" else ("❌ PROHRA" if item["status"] == "lost" else "⏳ ČEKÁ")
            lines.append(f"• Tiket #{item['id']}: *{status_emoji}*")
            lines.append(f"  _{item.get('result_detail', '')}_\n")
    else:
        lines.append("Dnes nebyly žádné otevřené tikety k vyhodnocení.\n")

    lines.append("📈 *Celková bilance systému:*")
    lines.append(f"• Odehráno tiketů: `{stats['won'] + stats['lost']}` (Čeká: `{stats['pending']}`)")
    lines.append(f"• Bilance: `{stats['won']}W - {stats['lost']}L` (Úspěšnost: `{stats['win_rate']}%`)")
    profit_sign = "+" if stats["profit"] >= 0 else ""
    lines.append(f"• Čistý zisk (1u/sázka): `{profit_sign}{stats['profit']}u`")
    lines.append(f"• ROI: `{profit_sign}{stats['roi']}%`")

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
    print("1/4 Načítám historii...")
    history = load_history()
    pending = [t for t in history if t.get("status") == "pending"]

    evaluated_summary = []
    if pending:
        print(f"2/4 Vyhodnocuji {len(pending)} otevřených tiketů...")
        eval_result = evaluate_pending_tickets(pending)
        if eval_result and "evaluations" in eval_result:
            eval_map = {item["id"]: item for item in eval_result["evaluations"]}
            for t in history:
                if t["id"] in eval_map:
                    new_status = eval_map[t["id"]].get("status", t["status"])
                    if new_status in ["won", "lost"]:
                        t["status"] = new_status
                        t["result_detail"] = eval_map[t["id"]].get("result_detail", "")
                        evaluated_summary.append({
                            "id": t["id"],
                            "status": new_status,
                            "result_detail": t["result_detail"]
                        })
            save_history(history)
    else:
        print("Žádné tikety nečekají na vyhodnocení.")

    print("3/4 Počítám celkové statistiky...")
    stats = calculate_stats(history)

    print("4/4 Odesílám souhrn na Telegram...")
    send_telegram(evaluated_summary, stats)
    print("Hotovo!")
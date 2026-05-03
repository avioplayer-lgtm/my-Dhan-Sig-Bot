# =========================
# WARRIOR v15 EXECUTION READY
# =========================

import os, time, uuid, threading, requests, pandas as pd, pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID   = os.environ.get("CHAT_ID")

DHAN_CLIENT_ID    = os.environ.get("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")

HEADERS = {
    "access-token": DHAN_ACCESS_TOKEN,
    "client-id": DHAN_CLIENT_ID,
    "Content-Type": "application/json"
}

IST = pytz.timezone("Asia/Kolkata")

SYMBOLS = {
    "NIFTY": "13",
    "BANKNIFTY": "25"
}

LOT_SIZE = {
    "NIFTY": 65,
    "BANKNIFTY": 15
}

state = {
    "active_trade": None,
    "pending_signals": {},
    "last_update_id": 0,
    "paused": False,
    "tsl_stage": 0,
    "last_trade": None,
    "reentry_count": 0,
    "max_reentries": 1,
    "daily_pnl": 0,
    "max_loss": -1500,
    "target_lock": 3000
}

# ================= TELEGRAM =================
def send(msg):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg}
    )

def send_signal(sig_id, sym, dire, strike, entry, sl, t1, t2):
    lot_cost = entry * LOT_SIZE[sym]

    msg = f"""
🚨 DHAN SIGNAL - {sym} {dire}
----------------------------

BUY: {sym} {strike} {dire}

Entry: ₹{round(entry,2)}
Stop Loss: ₹{round(sl,2)}

Targets:
T1: ₹{round(t1,2)}
T2: ₹{round(t2,2)}

Lot Cost: ₹{round(lot_cost,0)}

----------------------------
"""

    kb = {
        "inline_keyboard": [[
            {"text": "Take Trade", "callback_data": f"take|{sig_id}"},
            {"text": "Skip", "callback_data": f"skip|{sig_id}"}
        ]]
    }

    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "reply_markup": kb}
    )

# ================= DATA =================
def get_data(symbol):
    try:
        payload = {
            "securityId": SYMBOLS[symbol],
            "exchangeSegment": "IDX_I",
            "instrument": "INDEX",
            "interval": "5",
            "fromDate": datetime.now().date().isoformat(),
            "toDate": datetime.now().date().isoformat(),
        }

        r = requests.post(
            "https://api.dhan.co/v2/charts/intraday",
            headers=HEADERS,
            json=payload
        ).json()

        df = pd.DataFrame({
            "Close": r.get("close", []),
            "High": r.get("high", []),
            "Low": r.get("low", [])
        })

        if len(df) < 20:
            return None

        df["ema9"] = df["Close"].ewm(span=9).mean()
        df["ema21"] = df["Close"].ewm(span=21).mean()
        df["atr"] = (df["High"] - df["Low"]).rolling(10).mean()

        return df.dropna()

    except:
        return None

# ================= OPTION CHAIN =================
def get_option_chain(symbol):
    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS,
            json={"UnderlyingScrip": SYMBOLS[symbol]},
            timeout=10
        ).json()

        return r.get("data", {}).get("oc", [])
    except:
        return []

def select_strike(chain, direction):
    target = 0.5 if direction == "CE" else -0.5
    best = None
    diff = float("inf")

    for s in chain:
        opt = s.get(direction, {})
        delta = opt.get("delta")

        if delta is None:
            continue

        d = abs(delta - target)

        if d < diff:
            diff = d
            best = s.get("strikePrice")

    return best

def get_option_price(chain, strike, direction):
    for s in chain:
        if s.get("strikePrice") == strike:
            opt = s.get(direction, {})
            return float(opt.get("lastPrice", 0))
    return None

# ================= SIGNAL ENGINE =================
def run_scanner():
    if not is_valid_trading_time():
        return

    for symbol in ["NIFTY", "BANKNIFTY"]:

        df = get_data(symbol)
        if df is None:
            continue

        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema9, ema21, atr = last["ema9"], last["ema21"], last["atr"]

        if atr == 0 or pd.isna(atr):
            continue

        trend_up = ema9 > ema21
        trend_down = ema9 < ema21

        pullback_up = prev["Close"] < prev["ema9"] and last["Close"] > ema9
        pullback_dn = prev["Close"] > prev["ema9"] and last["Close"] < ema9

        direction = None

        if trend_up and pullback_up:
            direction = "CE"
        elif trend_down and pullback_dn:
            direction = "PE"

        if not direction:
            continue

        chain = get_option_chain(symbol)
        if not chain:
            continue

        strike = select_strike(chain, direction)
        option_price = get_option_price(chain, strike, direction)

        if not option_price or option_price < 10:
            continue

        entry = option_price
        sl = entry * 0.35
        t1 = entry * 1.65
        t2 = entry * 2.3

        sig_id = uuid.uuid4().hex[:6]

        state["pending_signals"][sig_id] = {
            "symbol": symbol,
            "direction": direction,
            "strike": strike,
            "entry": entry,
            "sl": sl,
            "t1": t1,
            "t2": t2,
            "atr": atr
        }

        send_signal(sig_id, symbol, direction, strike, entry, sl, t1, t2)

# ================= TIME FILTER =================
def is_valid_trading_time():
    now = datetime.now(IST)
    minutes = now.hour * 60 + now.minute

    return (
        (9*60+20 <= minutes <= 11*60+30) or
        (13*60+30 <= minutes <= 15*60+15)
    )

# ================= TELEGRAM LISTENER =================
def bot_listener():
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": state["last_update_id"]+1}
            ).json()

            for up in r.get("result", []):
                state["last_update_id"] = up["update_id"]

                if "callback_query" in up:
                    action, sig_id = up["callback_query"]["data"].split("|")

                    if action == "take":
                        state["active_trade"] = state["pending_signals"].pop(sig_id, None)
                        send("✅ Trade Activated")

                    elif action == "skip":
                        state["pending_signals"].pop(sig_id, None)

        except:
            time.sleep(5)

# ================= MAIN =================
def main():
    send("🚀 Warrior v15 LIVE")

    threading.Thread(target=bot_listener, daemon=True).start()

    while True:
        try:
            if state["daily_pnl"] <= state["max_loss"]:
                state["paused"] = True
                send("🛑 Max Loss Hit")
                time.sleep(60)
                continue

            if state["daily_pnl"] >= state["target_lock"]:
                state["paused"] = True
                send("💰 Target Achieved")
                time.sleep(60)
                continue

            if not state["paused"]:
                run_scanner()

        except Exception as e:
            print("ERROR:", e)

        time.sleep(60)

if __name__ == "__main__":
    main()

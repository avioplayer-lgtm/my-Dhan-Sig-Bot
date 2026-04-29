# =========================
# WARRIOR v14 INSTITUTIONAL
# =========================

import os, time, uuid, logging, threading, requests, pandas as pd, pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# CONFIG
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID   = os.environ.get("CHAT_ID")

DHAN_CLIENT_ID    = os.environ.get("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")

IST = pytz.timezone("Asia/Kolkata")

CAPITAL = 30000
MAX_DAILY_LOSS = 1500

SYMBOLS = {
    "NIFTY":     {"dhan_scrip": "13", "segment": "IDX_I"},
    "BANKNIFTY": {"dhan_scrip": "25", "segment": "IDX_I"},
}

HEADERS = {
    "access-token": DHAN_ACCESS_TOKEN,
    "client-id": DHAN_CLIENT_ID,
    "Content-Type": "application/json"
}

state = {
    "active_trade": None,
    "pending_signals": {},
    "last_update_id": 0,
    "paused": False,
    "daily_pnl": 0
}

# =========================
# DATA
# =========================

def get_data(symbol):
    today = datetime.now(IST).date().isoformat()
    payload = {
        "securityId": SYMBOLS[symbol]["dhan_scrip"],
        "exchangeSegment": "IDX_I",
        "instrument": "INDEX",
        "interval": "5",
        "fromDate": today,
        "toDate": today,
    }

    r = requests.post("https://api.dhan.co/v2/charts/intraday",
                      headers=HEADERS, json=payload).json()

    df = pd.DataFrame({
        "Close": r["close"],
        "High": r["high"],
        "Low": r["low"]
    })

    df["ema9"]  = df["Close"].ewm(span=9).mean()
    df["ema21"] = df["Close"].ewm(span=21).mean()
    df["atr"]   = (df["High"] - df["Low"]).rolling(10).mean()

    return df.dropna()

# =========================
# SMC LOGIC
# =========================

def detect_smc(df):
    prev_high = df["High"].iloc[-3]
    prev_low  = df["Low"].iloc[-3]
    curr_high = df["High"].iloc[-1]
    curr_low  = df["Low"].iloc[-1]

    if curr_high > prev_high:
        return "BOS_UP"
    elif curr_low < prev_low:
        return "BOS_DOWN"
    else:
        return "NONE"

# =========================
# OI ANALYSIS
# =========================

def get_option_chain(symbol):
    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS,
            json={"UnderlyingScrip": SYMBOLS[symbol]["dhan_scrip"]},
            timeout=10
        )

        data = r.json()

        # 🔍 PRINT WHAT YOU ARE ACTUALLY GETTING
        print("RAW OPTION CHAIN:", data)

        # ✅ Correct extraction
        chain = data.get("data", {}).get("oc", [])

        return chain

    except Exception as e:
        print("Option chain error:", e)
        return []

def analyze_oi(chain):
    if not isinstance(chain, list):
        return "NEUTRAL"

    total_call_oi = 0
    total_put_oi = 0

    for x in chain:
        try:
            ce = x.get("CE", {})
            pe = x.get("PE", {})

            total_call_oi += ce.get("openInterest", 0)
            total_put_oi  += pe.get("openInterest", 0)

        except Exception:
            continue

    if total_call_oi == 0:
        return "NEUTRAL"

    pcr = total_put_oi / total_call_oi

    if pcr > 1.2:
        return "BULLISH"
    elif pcr < 0.8:
        return "BEARISH"
    return "NEUTRAL"

# =========================
# DELTA STRIKE
# =========================

def select_strike(chain, direction):
    target = 0.5 if direction == "CE" else -0.5

    best = None
    min_diff = float("inf")

    for s in chain:
        try:
            opt = s["CE"] if direction == "CE" else s["PE"]
            delta = opt.get("delta")

            if delta is None:
                continue

            diff = abs(delta - target)

            if diff < min_diff:
                min_diff = diff
                best = s["strikePrice"]

        except:
            continue

    return best

# =========================
# TELEGRAM
# =========================

def send(msg):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                  json={"chat_id": CHAT_ID, "text": msg})

def send_signal(sig_id, symbol, direction, strike, entry, sl, tgt):
    msg = f"""
🚨 TRADE SIGNAL

{symbol} {strike} {direction}

Entry: {entry}
SL: {sl}
Target: {tgt}
"""

    kb = {
        "inline_keyboard": [[
            {"text": "Take Trade", "callback_data": f"take|{sig_id}"},
            {"text": "Skip", "callback_data": f"skip|{sig_id}"}
        ]]
    }

    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                  json={"chat_id": CHAT_ID, "text": msg, "reply_markup": kb})

# =========================
# SCANNER
# =========================

def run_scanner():

    chain = get_option_chain(symbol)
    if not chain:
    print("No chain data, skipping...")
    continue

    oi_bias = analyze_oi(chain)

    for symbol in ["NIFTY", "BANKNIFTY"]:
        df = get_data(symbol)
        if len(df) < 20:
            continue

        smc = detect_smc(df)
        last = df.iloc[-1]

        direction = None

        if smc == "BOS_UP" and oi_bias != "BEARISH":
            direction = "CE"

        elif smc == "BOS_DOWN" and oi_bias != "BULLISH":
            direction = "PE"

        if direction:
            strike = select_strike(chain, direction)

            entry = last["Close"]
            atr   = last["atr"]

            sl  = entry - atr if direction == "CE" else entry + atr
            tgt = entry + atr * 2 if direction == "CE" else entry - atr * 2

            sig_id = uuid.uuid4().hex[:6]

            state["pending_signals"][sig_id] = {
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "tgt": tgt
            }

            send_signal(sig_id, symbol, direction, strike, entry, sl, tgt)

# =========================
# MAIN LOOP
# =========================

def main():
    send("🚀 Warrior v14 Institutional Bot LIVE")

    while True:
        if not state["paused"]:
            run_scanner()

        time.sleep(300)

if __name__ == "__main__":
    main()

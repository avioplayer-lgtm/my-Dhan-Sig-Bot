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

def get_option_chain():
    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS,
            json={"UnderlyingScrip": "13"}  # NIFTY
        )
        return r.json()["data"]
    except:
        return None

def analyze_oi(chain):
    call_oi = sum([x["CE"]["openInterest"] for x in chain])
    put_oi  = sum([x["PE"]["openInterest"] for x in chain])

    pcr = put_oi / (call_oi + 1)

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
    diff = 999

    for s in chain:
        opt = s["CE"] if direction == "CE" else s["PE"]
        d = abs(opt.get("delta", 0) - target)

        if d < diff:
            diff = d
            best = s["strikePrice"]

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

    chain = get_option_chain()
    if not chain:
        return

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

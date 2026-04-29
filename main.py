# =========================
# WARRIOR v14 STABLE BUILD
# =========================

import os, time, uuid, logging, threading, requests, pandas as pd, pytz
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

state = {
    "active_trade": None,
    "pending_signals": {},
    "last_update_id": 0,
    "paused": False,
    "tsl_stage": 0
}

# ================= TELEGRAM =================
def send(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg})
    except:
        pass

def send_signal(sig_id, sym, dire, strike, entry, sl, tgt):
    msg = f"""
🚨 TRADE SIGNAL

{sym} {strike} {dire}

Entry: {round(entry,2)}
SL: {round(sl,2)}
Target: {round(tgt,2)}
"""

    kb = {
        "inline_keyboard": [[
            {"text": "Take Trade", "callback_data": f"take|{sig_id}"},
            {"text": "Skip", "callback_data": f"skip|{sig_id}"}
        ]]
    }

    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                  json={"chat_id": CHAT_ID, "text": msg, "reply_markup": kb})

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

        r = requests.post("https://api.dhan.co/v2/charts/intraday",
                          headers=HEADERS, json=payload).json()

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

    except Exception as e:
        print("DATA ERROR:", e)
        return None

# ================= SMC =================
def detect_smc(df):
    try:
        if df["High"].iloc[-1] > df["High"].iloc[-3]:
            return "BOS_UP"
        elif df["Low"].iloc[-1] < df["Low"].iloc[-3]:
            return "BOS_DOWN"
        return None
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
        )

        data = r.json()

        if not isinstance(data, dict):
            return []

        oc = data.get("data", {}).get("oc", [])

        if not isinstance(oc, list):
            return []

        return oc

    except Exception as e:
        print("OC ERROR:", e)
        return []

def analyze_oi(chain):
    try:
        call_oi = 0
        put_oi = 0

        for x in chain:
            ce = x.get("CE", {})
            pe = x.get("PE", {})

            call_oi += ce.get("openInterest", 0)
            put_oi += pe.get("openInterest", 0)

        if call_oi == 0:
            return "NEUTRAL"

        pcr = put_oi / call_oi

        if pcr > 1.2:
            return "BULLISH"
        elif pcr < 0.8:
            return "BEARISH"
        return "NEUTRAL"

    except:
        return "NEUTRAL"

def select_strike(chain, direction):
    try:
        target = 0.5 if direction == "CE" else -0.5
        best = None
        diff = float("inf")

        for s in chain:
            opt = s.get("CE") if direction == "CE" else s.get("PE")
            if not opt:
                continue

            delta = opt.get("delta")
            if delta is None:
                continue

            d = abs(delta - target)

            if d < diff:
                diff = d
                best = s.get("strikePrice")

        return best if best else "ATM"

    except:
        return "ATM"

# ================= TSL =================
def monitor_trade():
    t = state["active_trade"]
    if not t:
        return

    try:
        df = get_data(t["symbol"])
        if df is None:
            return

        price = df.iloc[-1]["Close"]
        atr = t["atr"]
        move = price - t["entry"] if t["direction"] == "CE" else t["entry"] - price

        # Move SL to cost
        if state["tsl_stage"] == 0 and move > atr * 0.3:
            state["tsl_stage"] = 1
            t["sl"] = t["entry"]
            send("SL moved to cost")

        # Target hit
        if (price >= t["tgt"] and t["direction"]=="CE") or (price <= t["tgt"] and t["direction"]=="PE"):
            send("Target Hit")
            state["active_trade"] = None
            state["tsl_stage"] = 0
            return

        # SL hit
        if (price <= t["sl"] and t["direction"]=="CE") or (price >= t["sl"] and t["direction"]=="PE"):
            send("SL Hit")
            state["active_trade"] = None
            state["tsl_stage"] = 0
            return

    except Exception as e:
        print("TSL ERROR:", e)

# ================= TELEGRAM LISTENER =================
def bot_listener():
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                             params={"offset": state["last_update_id"]+1}).json()

            for up in r.get("result", []):
                state["last_update_id"] = up["update_id"]

                if "callback_query" in up:
                    data = up["callback_query"]["data"].split("|")
                    action, sig_id = data

                    if action == "take":
                        state["active_trade"] = state["pending_signals"].pop(sig_id, None)
                        send("Trade Activated")

                    elif action == "skip":
                        state["pending_signals"].pop(sig_id, None)

        except:
            time.sleep(5)

# ================= SCANNER =================
def run_scanner():
    try:
        print("🔍 Scanner cycle running...")

        for symbol in ["NIFTY", "BANKNIFTY"]:

            print(f"\nChecking {symbol}...")

            # ===== GET PRICE DATA =====
            df = get_data(symbol)
            if df is None or len(df) < 20:
                print(f"{symbol}: Not enough data")
                continue

            # ===== SMC DETECTION =====
            smc = detect_smc(df)
            print(f"{symbol}: SMC = {smc}")

            if not smc:
                continue

            # ===== OPTION CHAIN =====
            chain = get_option_chain(symbol)

            if not chain:
                print(f"{symbol}: No option chain data")
                oi_bias = "NEUTRAL"
            else:
                oi_bias = analyze_oi(chain)

            print(f"{symbol}: OI Bias = {oi_bias}")

            # ===== DIRECTION DECISION =====
            direction = None

            # 🔥 Slightly aggressive logic (better signal frequency)
            if smc == "BOS_UP":
                direction = "CE"

            elif smc == "BOS_DOWN":
                direction = "PE"

            if not direction:
                print(f"{symbol}: No direction")
                continue

            # ===== STRIKE SELECTION =====
            strike = "ATM"

            if chain:
                strike = select_strike(chain, direction)

            print(f"{symbol}: Direction = {direction}, Strike = {strike}")

            # ===== ENTRY / SL / TARGET =====
            last = df.iloc[-1]
            entry = float(last["Close"])
            atr   = float(last["atr"])

            if atr == 0 or pd.isna(atr):
                print(f"{symbol}: Invalid ATR")
                continue

            sl  = entry - atr if direction == "CE" else entry + atr
            tgt = entry + (2 * atr) if direction == "CE" else entry - (2 * atr)

            # ===== CREATE SIGNAL =====
            sig_id = uuid.uuid4().hex[:6]

            state["pending_signals"][sig_id] = {
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "tgt": tgt,
                "atr": atr
            }

            print(f"✅ SIGNAL: {symbol} {direction} @ {entry}")

            # ===== SEND TELEGRAM SIGNAL =====
            send_signal(sig_id, symbol, direction, strike, entry, sl, tgt)

    except Exception as e:
        print("❌ SCANNER ERROR:", e)

# ================= MAIN =================
def main():
    print("BOT STARTED")
    send("🚀 Warrior v14 LIVE")

    threading.Thread(target=bot_listener, daemon=True).start()

    while True:
        try:
            if state["active_trade"]:
                monitor_trade()
            elif not state["paused"]:
                run_scanner()

        except Exception as e:
            print("MAIN ERROR:", e)

        time.sleep(60)

if __name__ == "__main__":
    main()

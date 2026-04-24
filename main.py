import os
import time
import json
import logging
import threading
import requests
import pandas as pd
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# CONFIG & 2026 PARAMETERS
# ─────────────────────────────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
CHAT_ID           = os.environ.get("CHAT_ID")
DHAN_CLIENT_ID    = (os.environ.get("DHAN_CLIENT_ID") or "").strip()
DHAN_ACCESS_TOKEN = (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()

IST = pytz.timezone("Asia/Kolkata")
SAFE_MODE_TRIGGER = 4000.0  # Secures profit after 4k
FLEXIBLE_TARGET   = 5000.0

# 2026 Standard Lot Sizes
SYMBOLS = {
    "NIFTY":     {"interval": 50,  "lot": 65,  "dhan_scrip": "13", "ws_scrip": 13, "segment": "IDX_I", "inst": "INDEX"},
    "BANKNIFTY": {"interval": 100, "lot": 30,  "dhan_scrip": "25", "ws_scrip": 25, "segment": "IDX_I", "inst": "INDEX"},
    # If you want the bot to also track Commodities (MCX)
    "CRUDEOIL":  {"interval": 100, "lot": 100, "dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
    "NATURALGAS":{"interval": 5,   "lot": 1250,"dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
}

DHAN_HEADERS = {"access-token": DHAN_ACCESS_TOKEN, "client-id": DHAN_CLIENT_ID, "Content-Type": "application/json"}
SCRIP_FILE   = "dhan_scrip_master.csv"

# ─────────────────────────────────────────
# GLOBAL STATE (RAM-BASED)
# ─────────────────────────────────────────
state = {
    "daily_pnl": 0.0,
    "is_sureshot": False,
    "active_trade": None,
    "paused": False,
    "last_update_id": 0,
    "breakeven_alerted": False
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("RailwayV10")

# ─────────────────────────────────────────
# INDICATORS & LOGIC
# ─────────────────────────────────────────
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def get_data(name):
    cfg = SYMBOLS[name]
    try:
        payload = {
            "securityId": cfg["dhan_scrip"], "exchangeSegment": cfg["segment"],
            "instrument": cfg["inst"], "interval": "5", 
            "fromDate": datetime.now(IST).date().isoformat(), 
            "toDate": datetime.now(IST).date().isoformat()
        }
        resp = requests.post("https://api.dhan.co/v2/charts/intraday", headers=DHAN_HEADERS, json=payload, timeout=10)
        d = resp.json()
        df = pd.DataFrame({"Close": d["close"], "High": d["high"], "Low": d["low"], "Volume": d["volume"]},
                          index=pd.to_datetime(d["timestamp"], unit="s", utc=True).tz_convert(IST))
        df['ema9'] = df['Close'].ewm(span=9, adjust=False).mean()
        df['ema21'] = df['Close'].ewm(span=21, adjust=False).mean()
        df['rsi'] = calculate_rsi(df['Close'])
        df['atr'] = (df['High'] - df['Low']).rolling(10).mean()
        return df.dropna()
    except: return None

# ─────────────────────────────────────────
# TELEGRAM COMMAND HANDLER
# ─────────────────────────────────────────
def send_text(txt):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": txt, "parse_mode": "Markdown"})

def bot_listener():
    """Listens for manual P&L sync or status requests."""
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={state['last_update_id'] + 1}", timeout=10).json()
            for up in r.get("result", []):
                state['last_update_id'] = up['update_id']
                msg = up.get("message", {}).get("text", "")
                
                if "/status" in msg:
                    mode = "🛡️ SURESHOT" if state["is_sureshot"] else "🚀 NORMAL"
                    active = state["active_trade"]["symbol"] if state["active_trade"] else "None"
                    send_text(f"*SYSTEM STATUS*\nMode: {mode}\nDaily P&L: ₹{state['daily_pnl']}\nActive Trade: {active}")
                
                if "/setpnl" in msg:
                    try:
                        val = float(msg.split(" ")[1])
                        state["daily_pnl"] = val
                        if val >= SAFE_MODE_TRIGGER: state["is_sureshot"] = True
                        send_text(f"✅ P&L Synced to ₹{val}. Mode updated.")
                    except: send_text("Format: `/setpnl 2500`")
                    
        except: pass
        time.sleep(5)

# ─────────────────────────────────────────
# MAIN TRADING ENGINE
# ─────────────────────────────────────────
def trading_cycle():
    # Update Scrip IDs at start
    # ... [Insert update_symbols_from_master logic here] ...
    
    while True:
        now = datetime.now(IST)
        # Check window (NSE Morning to MCX Night)
        if 9 * 60 <= now.hour * 60 + now.minute <= 23 * 30:
            
            # Sureshot Transition
            if state["daily_pnl"] >= SAFE_MODE_TRIGGER and not state["is_sureshot"]:
                state["is_sureshot"] = True
                send_text("⚠️ *TARGET REACHED*\nBot is now in Sureshot Mode (EMA + RSI + Vol confirmation required).")

            # Scan and Monitor (logic from v8/v9)
            # ...
            
        time.sleep(30)

if __name__ == "__main__":
    # Start Telegram Listener in background
    threading.Thread(target=bot_listener, daemon=True).start()
    send_text("🔋 *v10 ONLINE*\nRailway instance active. Use `/setpnl` if this was a mid-day restart.")
    trading_cycle()

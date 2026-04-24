import os
import time
import uuid
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
SAFE_MODE_TRIGGER = 4000.0  # Transition to Sureshot after 4k profit
FLEXIBLE_TARGET   = 5000.0
MAX_DAILY_LOSS    = 1500.0

# 2026 Lot Sizes & Required Segment Tags for v10
SYMBOLS = {
    "NIFTY":     {"interval": 50,  "lot": 65,  "dhan_scrip": "13", "ws_scrip": 13, "segment": "IDX_I", "inst": "INDEX"},
    "BANKNIFTY": {"interval": 100, "lot": 30,  "dhan_scrip": "25", "ws_scrip": 25, "segment": "IDX_I", "inst": "INDEX"},
    "CRUDEOIL":  {"interval": 100, "lot": 100, "dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
    "NATURALGAS":{"interval": 5,   "lot": 1250,"dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
}

DHAN_HEADERS     = {"access-token": DHAN_ACCESS_TOKEN, "client-id": DHAN_CLIENT_ID, "Content-Type": "application/json"}
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_FILE_PATH  = "dhan_scrip_master.csv"

# ─────────────────────────────────────────
# GLOBAL STATE (RAM-BASED for Railway)
# ─────────────────────────────────────────
state = {
    "daily_pnl": 0.0,
    "is_sureshot": False,
    "active_trade": None,
    "last_update_id": 0,
    "breakeven_alerted": False,
    "paused": False
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("RailwayV10")

# ─────────────────────────────────────────
# AUTO-FETCH SCRIP IDS
# ─────────────────────────────────────────
def update_symbols_from_master():
    log.info("Refreshing Scrip Master for active contracts...")
    try:
        if os.path.exists(SCRIP_FILE_PATH):
            age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(SCRIP_FILE_PATH))
            if age > timedelta(hours=24): os.remove(SCRIP_FILE_PATH)
        
        if not os.path.exists(SCRIP_FILE_PATH):
            r = requests.get(SCRIP_MASTER_URL, timeout=30)
            with open(SCRIP_FILE_PATH, 'wb') as f: f.write(r.content)
            
        df = pd.read_csv(SCRIP_FILE_PATH, low_memory=False)
        for name, cfg in SYMBOLS.items():
            if cfg["segment"] == "MCX_COMM":
                subset = df[(df['SEM_INSTRUMENT_NAME'] == 'FUTCOM') & (df['SEM_TRADING_SYMBOL'].str.contains(name))].copy()
                if not subset.empty:
                    subset['SEM_EXPIRY_DATE'] = pd.to_datetime(subset['SEM_EXPIRY_DATE'])
                    active = subset.sort_values('SEM_EXPIRY_DATE').iloc[0]
                    cfg["dhan_scrip"] = str(int(active['SEM_SMST_SECURITY_ID']))
                    cfg["ws_scrip"] = int(active['SEM_SMST_SECURITY_ID'])
                    log.info(f"Set {name} -> {cfg['dhan_scrip']} ({active['SEM_TRADING_SYMBOL']})")
    except Exception as e: log.error(f"Master update failed: {e}")

# ─────────────────────────────────────────
# DHAN DATA & INDICATORS
# ─────────────────────────────────────────
def get_ltp(name):
    try:
        cfg = SYMBOLS[name]
        seg_key = "NSE_INDEX" if cfg["segment"] == "IDX_I" else "MCX_COMM"
        resp = requests.post("https://api.dhan.co/v2/marketfeed/ltp", headers=DHAN_HEADERS, json={seg_key: [cfg["ws_scrip"]]}, timeout=5)
        return float(resp.json()["data"][seg_key][str(cfg["ws_scrip"])]["last_price"])
    except: return None

def get_data(name):
    cfg = SYMBOLS[name]
    today = datetime.now(IST).date().isoformat()
    try:
        payload = {
            "securityId": cfg["dhan_scrip"], "exchangeSegment": cfg["segment"],
            "instrument": cfg["inst"], "interval": "5", "fromDate": today, "toDate": today
        }
        resp = requests.post("https://api.dhan.co/v2/charts/intraday", headers=DHAN_HEADERS, json=payload, timeout=10)
        d = resp.json()
        df = pd.DataFrame({"Close": d["close"], "High": d["high"], "Low": d["low"], "Volume": d["volume"]},
                          index=pd.to_datetime(d["timestamp"], unit="s", utc=True).tz_convert(IST))
        # Indicators
        df['ema9'] = df['Close'].ewm(span=9, adjust=False).mean()
        df['ema21'] = df['Close'].ewm(span=21, adjust=False).mean()
        df['atr'] = (df['High'] - df['Low']).rolling(10).mean()
        # RSI Calculation
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss.replace(0, 1e-9))))
        return df.dropna()
    except Exception as e:
        log.error(f"Data fetch error {name}: {e}")
        return None

# ─────────────────────────────────────────
# TELEGRAM LISTENERS
# ─────────────────────────────────────────
def send_text(txt):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": txt, "parse_mode": "Markdown"})

def bot_listener():
    log.info("Telegram command listener started.")
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={state['last_update_id'] + 1}", timeout=10).json()
            for up in r.get("result", []):
                state['last_update_id'] = up['update_id']
                msg = up.get("message", {}).get("text", "")
                
                if "/status" in msg:
                    mode = "🛡️ SURESHOT" if state["is_sureshot"] else "🚀 NORMAL"
                    at = state["active_trade"]["symbol"] if state["active_trade"] else "None"
                    send_text(f"*SYSTEM STATUS*\nMode: {mode}\nDaily P&L: ₹{state['daily_pnl']}\nActive Trade: {at}")
                
                if "/setpnl" in msg:
                    try:
                        val = float(msg.split(" ")[1])
                        state["daily_pnl"] = val
                        state["is_sureshot"] = (val >= SAFE_MODE_TRIGGER)
                        send_text(f"✅ P&L Synced to ₹{val}. Mode: {'Sureshot' if state['is_sureshot'] else 'Normal'}")
                    except: send_text("Format: `/setpnl 2500`")
        except: pass
        time.sleep(5)

# ─────────────────────────────────────────
# TRADING SCANNER
# ─────────────────────────────────────────
def trading_cycle():
    update_symbols_from_master()
    send_text("🔋 *v10 SCANNER ACTIVE*\nMonitoring Nifty, BankNifty & MCX.")
    
    while True:
        now = datetime.now(IST)
        # Trading Window (9:15 AM to 11:30 PM)
        if 9 * 60 + 15 <= now.hour * 60 + now.minute <= 23 * 30:
            
            # Sureshot Transition
            if state["daily_pnl"] >= SAFE_MODE_TRIGGER and not state["is_sureshot"]:
                state["is_sureshot"] = True
                send_text("⚠️ *SWITCHING TO SURESHOT MODE*\nCriteria: EMA Cross + RSI (55-70/30-45) + Volume Confirmation.")

            # Monitor active trade
            if state["active_trade"]:
                monitor_active_trade()
            
            # Scan for new signals every 5 minutes
            elif now.minute % 5 == 0 and now.second < 20 and not state["paused"]:
                run_scanner()

        time.sleep(20)

def run_scanner():
    for name, cfg in SYMBOLS.items():
        if cfg["dhan_scrip"] is None: continue
        df = get_data(name)
        if df is None or len(df) < 21: continue
        
        last = df.iloc[-1]
        dire = None
        # Safe Sureshot Logic: EMA + RSI + Vol
        if last['ema9'] > last['ema21'] and last['rsi'] > 55 and last['Volume'] > df['Volume'].tail(5).mean():
            dire = "CE"
        elif last['ema9'] < last['ema21'] and last['rsi'] < 45 and last['Volume'] > df['Volume'].tail(5).mean():
            dire = "PE"

        if dire:
            entry = last['Close']
            sl = entry - (last['atr'] * 0.8) if dire == "CE" else entry + (last['atr'] * 0.8)
            tgt = entry + (last['atr'] * 1.5) if dire == "CE" else entry - (last['atr'] * 1.5)
            
            state["active_trade"] = {"symbol": name, "direction": dire, "entry": entry, "sl": sl, "tgt": tgt, "atr": last['atr']}
            send_text(f"🚀 *SIGNAL: {name} {dire}*\nEntry: {entry:.2f}\nTarget: {tgt:.2f}\nSL: {sl:.2f}\nMode: {'Sureshot' if state['is_sureshot'] else 'Normal'}")

def monitor_active_trade():
    t = state["active_trade"]
    live = get_ltp(t["symbol"])
    if not live: return

    # Move SL to Cost Suggestion
    move = (live - t["entry"]) if t["direction"] == "CE" else (t["entry"] - live)
    if not state["breakeven_alerted"] and move >= (t["atr"] * 0.4):
        state["breakeven_alerted"] = True
        send_text(f"🛡️ *SAFE PLAY*\n{t['symbol']} profit reached. Move SL to COST.")

    # Exit check
    hit_sl = (live <= t["sl"]) if t["direction"] == "CE" else (live >= t["sl"])
    hit_tgt = (live >= t["tgt"]) if t["direction"] == "CE" else (live <= t["tgt"])
    
    if hit_sl or hit_tgt:
        status = "🎯 TARGET" if hit_tgt else "🔴 SL/COST"
        send_text(f"*{status} HIT*\n{t['symbol']} at {live}. Check /exited.")
        state["active_trade"] = None
        state["breakeven_alerted"] = False

if __name__ == "__main__":
    threading.Thread(target=bot_listener, daemon=True).start()
    trading_cycle()

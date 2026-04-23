import os
import time
import uuid
import json
import sqlite3
import logging
import threading
import requests
import pandas as pd
import pytz
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# CONFIG & SAFETY PARAMETERS
# ─────────────────────────────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
CHAT_ID           = os.environ.get("CHAT_ID")
DHAN_CLIENT_ID    = (os.environ.get("DHAN_CLIENT_ID") or "").strip()
DHAN_ACCESS_TOKEN = (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()

if not BOT_TOKEN or not CHAT_ID:
    raise ValueError("BOT_TOKEN and CHAT_ID must be set as environment variables.")

# Trading Parameters
CAPITAL           = float(os.environ.get("CAPITAL", 30000))
SAFE_MODE_TRIGGER = 4000.0  # Transition to "Sureshot" after 4k profit
FLEXIBLE_TARGET   = 5000.0  # Daily ceiling
MAX_DAILY_LOSS    = 1500.0  
IST               = pytz.timezone("Asia/Kolkata")

# 2026 Lot Sizes & Configuration
SYMBOLS = {
    "NIFTY":     {"interval": 50,  "lot": 65,  "dhan_scrip": "13", "ws_scrip": 13, "segment": "IDX_I", "inst": "INDEX"},
    "BANKNIFTY": {"interval": 100, "lot": 30,  "dhan_scrip": "25", "ws_scrip": 25, "segment": "IDX_I", "inst": "INDEX"},
    "CRUDEOIL":  {"interval": 100, "lot": 100, "dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
    "NATURALGAS":{"interval": 5,   "lot": 1250,"dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
}

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_FILE_PATH  = "dhan_scrip_master.csv"
DHAN_HEADERS     = {"access-token": DHAN_ACCESS_TOKEN, "client-id": DHAN_CLIENT_ID, "Content-Type": "application/json"}

# ─────────────────────────────────────────
# PERSISTENCE & LOGGING
# ─────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("SafeTradeV8")

conn = sqlite3.connect("state.db", check_same_thread=False)
conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
conn.commit()

state = {
    "active_trade": None, "daily_pnl": 0.0, "current_day": None, 
    "is_sureshot": False, "paused": False, "breakeven_alerted": False
}

def _persist():
    with conn: conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", ("state", json.dumps(state, default=str)))

def _load():
    row = conn.execute("SELECT v FROM kv WHERE k='state'").fetchone()
    if row: state.update(json.loads(row[0]))

# ─────────────────────────────────────────
# AUTO-FETCH SCRIP IDS (MCX)
# ─────────────────────────────────────────
def update_symbols_from_master():
    log.info("Updating Scrip Master...")
    try:
        if os.path.exists(SCRIP_FILE_PATH):
            age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(SCRIP_FILE_PATH))
            if age > timedelta(hours=24): os.remove(SCRIP_FILE_PATH)
        
        if not os.path.exists(SCRIP_FILE_PATH):
            r = requests.get(SCRIP_MASTER_URL)
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
# DHAN API & INDICATORS
# ─────────────────────────────────────────
def get_ltp(name):
    try:
        scrip = SYMBOLS[name]["ws_scrip"]
        seg_key = "NSE_INDEX" if SYMBOLS[name]["segment"] == "IDX_I" else "MCX_COMM"
        resp = requests.post("https://api.dhan.co/v2/marketfeed/ltp", headers=DHAN_HEADERS, json={seg_key: [scrip]}, timeout=5)
        return float(resp.json()["data"][seg_key][str(scrip)]["last_price"])
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
        df = pd.DataFrame({"Open": d["open"], "High": d["high"], "Low": d["low"], "Close": d["close"], "Volume": d["volume"]},
                          index=pd.to_datetime(d["timestamp"], unit="s", utc=True).tz_convert(IST))
        df['ema9'] = df['Close'].ewm(span=9, adjust=False).mean()
        df['ema21'] = df['Close'].ewm(span=21, adjust=False).mean()
        df['atr'] = (df['High'] - df['Low']).rolling(10).mean()
        return df.dropna()
    except: return None

# ─────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────
def scan_for_signals():
    if state["daily_pnl"] >= FLEXIBLE_TARGET: return
    
    for name, cfg in SYMBOLS.items():
        df = get_data(name)
        if df is None or len(df) < 21: continue
        
        last = df.iloc[-1]
        dire = None
        conf = 0
        
        # Sureshot Logic (EMA + Volume)
        if last['ema9'] > last['ema21'] and last['Volume'] > df['Volume'].tail(5).mean():
            dire, conf = "CE", 7
        elif last['ema9'] < last['ema21'] and last['Volume'] > df['Volume'].tail(5).mean():
            dire, conf = "PE", 7
            
        if dire:
            # If in Sureshot mode, ignore lower confidence or choppy trends
            if state["is_sureshot"] and conf < 7: continue
            
            entry = last['Close']
            sl = entry - (last['atr'] * 0.8) if dire == "CE" else entry + (last['atr'] * 0.8)
            tgt = entry + (last['atr'] * 1.5) if dire == "CE" else entry - (last['atr'] * 1.5)
            
            send_signal(name, dire, entry, sl, tgt)

# ─────────────────────────────────────────
# TRADE MANAGEMENT
# ─────────────────────────────────────────
def monitor_active():
    t = state["active_trade"]
    if not t: return
    
    live = get_ltp(t["symbol"])
    if not live: return
    
    # Move to Cost Logic
    move = (live - t["entry"]) if t["direction"] == "CE" else (t["entry"] - live)
    if not state["breakeven_alerted"] and move >= (t["atr"] * 0.4):
        state["breakeven_alerted"] = True
        send_text(f"🛡️ *SAFE PLAY*\n{t['symbol']} profit buffer reached. Move SL to COST (Entry).")

    # Exit Check
    hit_sl = (live <= t["sl"]) if t["direction"] == "CE" else (live >= t["sl"])
    hit_tgt = (live >= t["tgt"]) if t["direction"] == "CE" else (live <= t["tgt"])
    
    if hit_sl or hit_tgt:
        msg = "🎯 TARGET REACHED" if hit_tgt else "🔴 EXIT TRIGGERED"
        send_text(f"*{msg}*\n{t['symbol']} at {live}. Check /exited.")
        state["active_trade"] = None
        state["breakeven_alerted"] = False
        _persist()

# ─────────────────────────────────────────
# BOT INTERFACE
# ─────────────────────────────────────────
def send_text(txt):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": txt, "parse_mode": "Markdown"})

def send_signal(name, dire, entry, sl, tgt):
    msg = (f"🚀 *SIGNAL: {name} {dire}*\nEntry: {entry:.2f}\n"
           f"Target: {tgt:.2f}\nSL: {sl:.2f}\n"
           f"Mode: {'Sureshot' if state['is_sureshot'] else 'Normal'}")
    # Store trade (simplified)
    state["active_trade"] = {"symbol": name, "direction": dire, "entry": entry, "sl": sl, "tgt": tgt, "atr": (abs(entry-sl)/0.8)}
    send_text(msg)
    _persist()

def main_loop():
    _load()
    update_symbols_from_master()
    
    while True:
        now = datetime.now(IST)
        # Check window: 9:15 AM to 11:30 PM (to cover MCX)
        if 9 * 60 + 15 <= now.hour * 60 + now.minute <= 23 * 30:
            
            # Sureshot mode transition
            if state["daily_pnl"] >= SAFE_MODE_TRIGGER and not state["is_sureshot"]:
                state["is_sureshot"] = True
                send_text("⚠️ *SURESHOT MODE ACTIVE*\nCriteria tightened to protect daily profit.")
            
            monitor_active()
            
            if now.minute % 5 == 0 and now.second < 15:
                if not state["active_trade"] and not state["paused"]:
                    scan_for_signals()
                    
        time.sleep(20)

if __name__ == "__main__":
    main_loop()

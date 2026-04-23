import os
import time
import uuid
import json
import sqlite3
import logging
import requests
import pandas as pd
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# CONFIG & PERSISTENCE PATHS
# ─────────────────────────────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
CHAT_ID           = os.environ.get("CHAT_ID")
DHAN_CLIENT_ID    = (os.environ.get("DHAN_CLIENT_ID") or "").strip()
DHAN_ACCESS_TOKEN = (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()

# Change database path to the mounted volume for 24/7 persistence
DB_DIR  = "/app/data" if os.path.exists("/app/data") else "."
DB_PATH = os.path.join(DB_DIR, "state.db")
SCRIP_FILE_PATH = os.path.join(DB_DIR, "dhan_scrip_master.csv")

IST = pytz.timezone("Asia/Kolkata")
CAPITAL = float(os.environ.get("CAPITAL", 30000))

SYMBOLS = {
    "NIFTY":     {"interval": 50,  "lot": 65,  "dhan_scrip": "13", "ws_scrip": 13, "segment": "IDX_I", "inst": "INDEX"},
    "BANKNIFTY": {"interval": 100, "lot": 30,  "dhan_scrip": "25", "ws_scrip": 25, "segment": "IDX_I", "inst": "INDEX"},
    "CRUDEOIL":  {"interval": 100, "lot": 100, "dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
    "NATURALGAS":{"interval": 5,   "lot": 1250, "dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
}

# ─────────────────────────────────────────
# STATE & DATABASE (One-shot setup)
# ─────────────────────────────────────────
def init_db():
    if not os.path.exists(DB_DIR): os.makedirs(DB_DIR)
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    c.commit()
    return c

conn = init_db()
state = {"active_trade": None, "daily_pnl": 0.0, "is_sureshot": False, "last_reset_date": ""}

def _persist():
    with conn: conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", ("state", json.dumps(state)))

def _load():
    row = conn.execute("SELECT v FROM kv WHERE k='state'").fetchone()
    if row: state.update(json.loads(row[0]))

# ─────────────────────────────────────────
# DAILY MAINTENANCE (The "Pre-Market" Routine)
# ─────────────────────────────────────────
def daily_maintenance():
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")

    # Reset PnL and refresh Scrips at 8:30 AM every day
    if state["last_reset_date"] != today_str:
        log.info(f"🌞 Good Morning. Performing daily maintenance for {today_str}...")
        
        # Reset Trading State
        state["daily_pnl"] = 0.0
        state["is_sureshot"] = False
        state["last_reset_date"] = today_str
        state["active_trade"] = None
        
        # Refresh Scrip Master for MCX Rollovers
        update_symbols_from_master()
        
        send_text(f"🔋 *BOT ONLINE: {today_str}*\nScrips updated. P&L reset to 0. Ready for the opening bell.")
        _persist()

# ─────────────────────────────────────────
# CORE EXECUTION
# ─────────────────────────────────────────
def main_loop():
    _load()
    log.info("🚀 24/7 Scalp Bot initialized.")
    
    while True:
        try:
            now = datetime.now(IST)
            
            # 1. Run Maintenance
            daily_maintenance()
            
            # 2. Market Monitoring (NSE/MCX Window)
            if 9 * 60 + 0 <= now.hour * 60 + now.minute <= 23 * 45:
                # [Previous monitor_active and scan_for_signals logic goes here]
                pass
            
            # 3. Heartbeat (Every hour outside market hours)
            elif now.minute == 0 and now.second < 30:
                log.info("💤 Market closed. Bot heartbeat active.")

        except Exception as e:
            log.error(f"⚠️ Runtime Error: {e}")
            time.sleep(60) # Wait and retry
            
        time.sleep(20)

if __name__ == "__main__":
    main_loop()

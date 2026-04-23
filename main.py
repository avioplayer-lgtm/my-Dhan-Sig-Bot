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
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

# ─────────────────────────────────────────
# CONFIG & TARGETS
# ─────────────────────────────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
CHAT_ID           = os.environ.get("CHAT_ID")
DHAN_CLIENT_ID    = (os.environ.get("DHAN_CLIENT_ID") or "").strip()
DHAN_ACCESS_TOKEN = (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()

CAPITAL           = float(os.environ.get("CAPITAL", 30000))
FLEXIBLE_TARGET   = 4500.0  # Mid-point of 4k-5k
SAFE_MODE_LIMIT   = 4000.0  # Trigger for "Sureshot Mode"
MAX_DAILY_LOSS    = 1500.0  # Hard stop to protect 30k capital
IST               = pytz.timezone("Asia/Kolkata")

SYMBOLS = {
    "NIFTY":     {"interval": 50,  "lot": 75, "dhan_scrip": "13", "ws_scrip": 13},
    "BANKNIFTY": {"interval": 100, "lot": 15, "dhan_scrip": "25", "ws_scrip": 25},
}

DHAN_HEADERS = {"access-token": DHAN_ACCESS_TOKEN, "client-id": DHAN_CLIENT_ID, "Content-Type": "application/json"}

# ─────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────
state = {
    "active_trade": None, 
    "daily_pnl": 0.0, 
    "is_sureshot_mode": False,
    "breakeven_alerted": False
}

def check_pnl_mode():
    if state["daily_pnl"] >= SAFE_MODE_LIMIT:
        if not state["is_sureshot_mode"]:
            state["is_sureshot_mode"] = True
            send_text("🛡️ *TARGET REACHED: SWITCHING TO SURESHOT MODE*\nScanning only high-probability setups now. Capital protection is priority.")

# ─────────────────────────────────────────
# STRATEGY: SURESHOT CRITERIA
# ─────────────────────────────────────────
def is_sureshot_setup(df, direction):
    last = df.iloc[-1]
    # Sureshot requires: EMA 9/21 cross + RSI confirmation + Volume surge
    ema_confirm = (last['ema9'] > last['ema21']) if direction == "CE" else (last['ema9'] < last['ema21'])
    rsi_confirm = (55 < last['rsi'] < 70) if direction == "CE" else (30 < last['rsi'] < 45)
    vol_confirm = last['Volume'] > df['Volume'].tail(5).mean()
    
    return all([ema_confirm, rsi_confirm, vol_confirm])

# ─────────────────────────────────────────
# ACTIVE MONITORING
# ─────────────────────────────────────────
def monitor_trade():
    trade = state.get("active_trade")
    if not trade: return
    
    live_idx = get_dhan_ltp(trade["symbol"])
    if not live_idx: return

    entry_idx = trade["entry_idx"]
    direction = trade["direction"]
    
    # Calculate % move in index
    move_pct = ((live_idx - entry_idx) / entry_idx) * 100 if direction == "CE" else ((entry_idx - live_idx) / entry_idx) * 100

    # 1. Automatic "Move to Cost" Suggestion
    if not state["breakeven_alerted"] and move_pct >= 0.25: # Roughly 15% move in ATM premium
        state["breakeven_alerted"] = True
        send_text(f"✅ *SECURE THE TRADE*\n{trade['symbol']} is moving well.\n*ACTION:* Move SL to COST now. Ensure Zero Loss.")

    # 2. Hard Exit Logic
    sl_hit = (live_idx <= trade["sl_idx"]) if direction == "CE" else (live_idx >= trade["sl_idx"])
    tgt_hit = (live_idx >= trade["tgt_idx"]) if direction == "CE" else (live_idx <= trade["tgt_idx"])

    if sl_hit or tgt_hit:
        status = "🎯 TARGET" if tgt_hit else "🔴 SL/COST"
        send_text(f"*{status} HIT*\nIndex: {live_idx}\nClosing trade state. Send /exited.")
        # Update daily PnL (estimated)
        pnl = (trade['tgt_prem'] - trade['atm_prem']) * trade['lot'] if tgt_hit else -150
        state["daily_pnl"] += pnl
        state["active_trade"] = None
        state["breakeven_alerted"] = False
        check_pnl_mode()

# ─────────────────────────────────────────
# DHAN API WRAPPERS
# ─────────────────────────────────────────
def get_dhan_ltp(name):
    try:
        scrip_id = SYMBOLS[name]["ws_scrip"]
        resp = requests.post("https://api.dhan.co/v2/marketfeed/ltp", headers=DHAN_HEADERS, json={"NSE_INDEX": [scrip_id]}, timeout=5)
        return float(resp.json()["data"]["NSE_INDEX"][str(scrip_id)]["last_price"])
    except: return None

def send_text(text):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"})

# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main():
    while True:
        now = datetime.now(IST)
        if 9 * 60 + 15 <= now.hour * 60 + now.minute <= 15 * 30:
            monitor_trade()
            # Scanning logic runs here...
        time.sleep(15)

if __name__ == "__main__":
    main()

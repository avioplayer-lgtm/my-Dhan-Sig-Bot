import os
import time
import uuid
import logging
import threading
import requests
import pandas as pd
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# CONFIG
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
CHAT_ID           = os.environ.get("CHAT_ID")
DHAN_CLIENT_ID    = (os.environ.get("DHAN_CLIENT_ID") or "").strip()
DHAN_ACCESS_TOKEN = (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()

IST               = pytz.timezone("Asia/Kolkata")
CAPITAL           = float(os.environ.get("CAPITAL", 30000))
SAFE_MODE_TRIGGER = 4000.0
MAX_DAILY_LOSS    = 1500.0

SYMBOLS = {
    "NIFTY":      {"lot": 65,   "dhan_scrip": "13", "ws_scrip": 13,  "segment": "IDX_I",    "inst": "INDEX",  "expiry_day": 3},
    "BANKNIFTY":  {"lot": 30,   "dhan_scrip": "25", "ws_scrip": 25,  "segment": "IDX_I",    "inst": "INDEX",  "expiry_day": 2},
    "CRUDEOIL":   {"lot": 100,  "dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM", "expiry_day": 0},
    "NATURALGAS": {"lot": 1250, "dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM", "expiry_day": 0},
}

DHAN_HEADERS = {
    "access-token": DHAN_ACCESS_TOKEN,
    "client-id":    DHAN_CLIENT_ID,
    "Content-Type": "application/json",
}

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_FILE_PATH  = "dhan_scrip_master.csv"

# LOGGING
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("WarriorV13")

# STATE
state = {
    "daily_pnl":       0.0,
    "is_sureshot":     False,
    "active_trade":    None,
    "last_update_id":  0,
    "tsl_stage":       0,
    "paused":          False,
    "pending_signals": {},
}

# SCRIP MASTER
def update_symbols_from_master():
    log.info("Refreshing Scrip Master...")
    try:
        if os.path.exists(SCRIP_FILE_PATH):
            age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(SCRIP_FILE_PATH))
            if age > timedelta(hours=24):
                os.remove(SCRIP_FILE_PATH)
        if not os.path.exists(SCRIP_FILE_PATH):
            r = requests.get(SCRIP_MASTER_URL, timeout=30)
            with open(SCRIP_FILE_PATH, "wb") as f:
                f.write(r.content)
        df = pd.read_csv(SCRIP_FILE_PATH, low_memory=False)
        for name, cfg in SYMBOLS.items():
            if cfg["segment"] == "MCX_COMM":
                subset = df[
                    (df["SEM_INSTRUMENT_NAME"] == "FUTCOM") &
                    (df["SEM_TRADING_SYMBOL"].str.contains(name, na=False))
                ].copy()
                if not subset.empty:
                    subset["SEM_EXPIRY_DATE"] = pd.to_datetime(subset["SEM_EXPIRY_DATE"])
                    active = subset.sort_values("SEM_EXPIRY_DATE").iloc[0]
                    cfg["dhan_scrip"] = str(int(active["SEM_SMST_SECURITY_ID"]))
                    cfg["ws_scrip"]   = int(active["SEM_SMST_SECURITY_ID"])
                    log.info("Set %s -> %s (%s)", name, cfg["dhan_scrip"], active["SEM_TRADING_SYMBOL"])
    except Exception as e:
        log.error("Master update failed: %s", e)

# DHAN DATA
def get_next_expiry(name):
    now        = datetime.now(IST)
    target_day = SYMBOLS[name]["expiry_day"]
    days_ahead = target_day - now.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return (now + timedelta(days=days_ahead)).strftime("%d %b").upper()

def get_data(name):
    cfg   = SYMBOLS[name]
    today = datetime.now(IST).date().isoformat()
    try:
        payload = {
            "securityId":      cfg["dhan_scrip"],
            "exchangeSegment": cfg["segment"],
            "instrument":      cfg["inst"],
            "interval":        "5",
            "fromDate":        today,
            "toDate":          today,
        }
        resp = requests.post(
            "https://api.dhan.co/v2/charts/intraday",
            headers=DHAN_HEADERS,
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        d  = resp.json()
        df = pd.DataFrame(
            {"Close": d["close"], "High": d["high"], "Low": d["low"], "Volume": d["volume"]},
            index=pd.to_datetime(d["timestamp"], unit="s", utc=True).tz_convert(IST),
        )
        df["ema9"]  = df["Close"].ewm(span=9,  adjust=False).mean()
        df["ema21"] = df["Close"].ewm(span=21, adjust=False).mean()
        df["atr"]   = (df["High"] - df["Low"]).rolling(10).mean()
        delta       = df["Close"].diff()
        gain        = delta.where(delta > 0, 0).rolling(14).mean()
        loss        = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df["rsi"]   = 100 - (100 / (1 + gain / loss.replace(0, 1e-9)))
        return df.dropna()
    except Exception as e:
        log.error("get_data %s: %s", name, e)
        return None

def get_ltp(name):
    try:
        cfg     = SYMBOLS[name]
        seg_key = "NSE_INDEX" if cfg["segment"] == "IDX_I" else "MCX_COMM"
        resp    = requests.post(
            "https://api.dhan.co/v2/marketfeed/ltp",
            headers=DHAN_HEADERS,
            json={seg_key: [cfg["ws_scrip"]]},
            timeout=5,
        )
        return float(resp.json()["data"][seg_key][str(cfg["ws_scrip"])]["last_price"])
    except Exception as e:
        log.error("get_ltp %s: %s", name, e)
        return None

# TELEGRAM
def send_text(txt):
    try:
        requests.post(
            "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage",
            json={"chat_id": CHAT_ID, "text": txt, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.error("send_text: %s", e)

def send_signal_with_buttons(sig_id, name, dire, entry, sl, tgt):
    expiry   = get_next_expiry(name) if SYMBOLS[name]["segment"] == "IDX_I" else "MONTHLY"
    mode_lbl = "Sureshot" if state["is_sureshot"] else "Normal"
    msg = (
        "*BUY " + name + " " + dire + "*\n"
        "Expiry    : " + expiry + "\n"
        "----------------------------\n"
        "Buy At    : " + "{:.2f}".format(entry) + "\n"
        "Target    : " + "{:.2f}".format(tgt) + "\n"
        "Stop Loss : " + "{:.2f}".format(sl) + "\n"
        "----------------------------\n"
        "Mode      : " + mode_lbl + "\n"
        "Bot manages TSL and Targets."
    )
    kb = {
        "inline_keyboard": [[
            {"text": "Take Trade", "callback_data": "take|" + sig_id},
            {"text": "Skip",       "callback_data": "skip|" + sig_id},
        ]]
    }
    try:
        requests.post(
            "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown", "reply_markup": kb},
            timeout=10,
        )
    except Exception as e:
        log.error("send_signal_with_buttons: %s", e)

def bot_listener():
    log.info("Telegram command listener active.")
    while True:
        try:
            r = requests.get(
                "https://api.telegram.org/bot" + BOT_TOKEN + "/getUpdates",
                params={"offset": state["last_update_id"] + 1, "timeout": 25},
                timeout=30,
            ).json()
            for up in r.get("result", []):
                state["last_update_id"] = up["update_id"]

                if "callback_query" in up:
                    cb     = up["callback_query"]
                    parts  = cb["data"].split("|")
                    action = parts[0]
                    sig_id = parts[1] if len(parts) > 1 else ""
                    if action == "take" and sig_id in state["pending_signals"]:
                        state["active_trade"] = state["pending_signals"].pop(sig_id)
                        state["tsl_stage"]    = 0
                        sym = state["active_trade"]["symbol"]
                        send_text("Trade Confirmed: Monitoring " + sym + "...")
                        log.info("Trade taken: %s %s", sym, state["active_trade"]["direction"])
                    elif action == "skip":
                        state["pending_signals"].pop(sig_id, None)
                        send_text("Signal skipped.")

                msg = up.get("message", {}).get("text", "")
                if not msg:
                    continue

                if "/status" in msg:
                    mode = "SURESHOT" if state["is_sureshot"] else "NORMAL"
                    at   = state["active_trade"]["symbol"] if state["active_trade"] else "None"
                    send_text(
                        "*WARRIOR STATUS*\n"
                        "Mode      : " + mode + "\n"
                        "Daily P&L : Rs." + "{:.0f}".format(state["daily_pnl"]) + "\n"
                        "Active    : " + at + "\n"
                        "TSL Stage : " + str(state["tsl_stage"]) + "\n"
                        "Paused    : " + str(state["paused"])
                    )
                elif "/setpnl" in msg:
                    try:
                        val = float(msg.split(" ")[1])
                        state["daily_pnl"]   = val
                        state["is_sureshot"] = val >= SAFE_MODE_TRIGGER
                        lbl = "Sureshot" if state["is_sureshot"] else "Normal"
                        send_text("P&L set to Rs." + "{:.0f}".format(val) + ". Mode: " + lbl)
                    except Exception:
                        send_text("Format: /setpnl 2500")
                elif "/exited" in msg:
                    at  = state["active_trade"]
                    sym = at["symbol"] if at else "None"
                    state["active_trade"] = None
                    state["tsl_stage"]    = 0
                    send_text("Trade cleared: " + sym + ". Ready for next signal.")
                    log.info("Trade manually exited: %s", sym)
                elif "/pause" in msg:
                    state["paused"] = True
                    send_text("Bot scanning PAUSED. Use /resume to restart.")
                elif "/resume" in msg:
                    state["paused"] = False
                    send_text("Bot scanning RESUMED.")
                elif "/cancel" in msg:
                    count = len(state["pending_signals"])
                    state["pending_signals"] = {}
                    send_text("Cancelled " + str(count) + " pending signal(s).")
                elif "/help" in msg:
                    send_text(
                        "*Warrior v13 Commands*\n\n"
                        "/status  - Bot state, mode, active trade\n"
                        "/setpnl  - Sync daily P&L e.g. /setpnl 2500\n"
                        "/exited  - Manually close active trade\n"
                        "/pause   - Pause signal scanning\n"
                        "/resume  - Resume signal scanning\n"
                        "/cancel  - Cancel pending signals\n"
                        "/help    - This list"
                    )
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.error("bot_listener error: %s", e)
            time.sleep(5)

# TSL ENGINE
def monitor_active_trade():
    t = state["active_trade"]
    if not t:
        return
    live = get_ltp(t["symbol"])
    if live is None:
        log.warning("LTP unavailable for %s", t["symbol"])
        return
    atr  = t["atr"]
    dire = t["direction"]
    move = (live - t["entry"]) if dire == "CE" else (t["entry"] - live)

    if state["tsl_stage"] == 0 and move >= atr * 0.3:
        state["tsl_stage"] = 1
        t["sl"] = t["entry"]
        send_text("SAFE PLAY\n" + t["symbol"] + " in green. SL moved to cost " + "{:.2f}".format(t["entry"]) + ".")
        log.info("TSL Stage 1: %s SL -> cost %.2f", t["symbol"], t["entry"])

    df = None
    if move >= atr * 1.2:
        df = get_data(t["symbol"])
        if df is not None:
            state["tsl_stage"] = 3
            new_sl = float(df.iloc[-1]["ema9"])
            t["sl"] = max(t["sl"], new_sl) if dire == "CE" else min(t["sl"], new_sl)
            log.info("TSL Stage 3: %s EMA trail SL -> %.2f", t["symbol"], t["sl"])

    hit_tgt = (live >= t["tgt"]) if dire == "CE" else (live <= t["tgt"])
    if hit_tgt:
        if df is None:
            df = get_data(t["symbol"])
        rsi_val   = float(df.iloc[-1]["rsi"]) if df is not None else 50
        is_strong = rsi_val > 65 if dire == "CE" else rsi_val < 35
        if is_strong and state["tsl_stage"] < 4:
            state["tsl_stage"] = 4
            old_tgt  = t["tgt"]
            t["tgt"] = old_tgt + atr * 0.5 if dire == "CE" else old_tgt - atr * 0.5
            t["sl"]  = old_tgt
            send_text(
                "MOMENTUM EXTENSION\n"
                + t["symbol"] + " target hit. RSI=" + "{:.0f}".format(rsi_val) + " strong.\n"
                + "New Target : " + "{:.2f}".format(t["tgt"]) + "\n"
                + "SL locked  : " + "{:.2f}".format(old_tgt)
            )
            log.info("TSL Stage 4: %s extended tgt=%.2f", t["symbol"], t["tgt"])
            return
        else:
            send_text("TARGET HIT\n" + t["symbol"] + " at " + "{:.2f}".format(live) + ".\nSend /exited to reset.")
            log.info("Target hit: %s at %.2f", t["symbol"], live)
            state["active_trade"] = None
            state["tsl_stage"]    = 0
            return

    hit_sl = (live <= t["sl"]) if dire == "CE" else (live >= t["sl"])
    if hit_sl:
        send_text("EXIT SIGNAL\n" + t["symbol"] + " SL/TSL hit at " + "{:.2f}".format(live) + ".\nSend /exited to reset.")
        log.info("SL hit: %s at %.2f", t["symbol"], live)
        state["active_trade"] = None
        state["tsl_stage"]    = 0
        return

    log.info("Monitor %s: live=%.2f | SL=%.2f | Tgt=%.2f | TSL=%d",
             t["symbol"], live, t["sl"], t["tgt"], state["tsl_stage"])

# PRIORITY SCANNER
def run_scanner():
    now           = datetime.now(IST)
    mins          = now.hour * 60 + now.minute
    is_nse_window = (9 * 60 + 15) <= mins <= (15 * 60 + 30)
    scan_list     = ["NIFTY", "BANKNIFTY"] if is_nse_window else ["CRUDEOIL", "NATURALGAS"]
    log.info("Scanner: %s | %s window", scan_list, "NSE" if is_nse_window else "MCX")

    for name in scan_list:
        cfg = SYMBOLS[name]
        if cfg["dhan_scrip"] is None:
            log.warning("%s: scrip ID not set - skipping", name)
            continue
        df = get_data(name)
        if df is None or len(df) < 21:
            continue
        last    = df.iloc[-1]
        vol_avg = df["Volume"].tail(5).mean()
        dire    = None
        if last["ema9"] > last["ema21"] and last["rsi"] > 55 and last["Volume"] > vol_avg:
            dire = "CE"
        elif last["ema9"] < last["ema21"] and last["rsi"] < 45 and last["Volume"] > vol_avg:
            dire = "PE"

        if dire:
            entry  = float(last["Close"])
            atr    = float(last["atr"])
            sl     = entry - atr * 0.8 if dire == "CE" else entry + atr * 0.8
            tgt    = entry + atr * 1.5 if dire == "CE" else entry - atr * 1.5
            sig_id = uuid.uuid4().hex[:6]
            state["pending_signals"][sig_id] = {
                "symbol": name, "direction": dire, "entry": entry,
                "sl": sl, "tgt": tgt, "atr": atr, "segment": cfg["segment"],
            }
            send_signal_with_buttons(sig_id, name, dire, entry, sl, tgt)
            log.info("Signal: %s %s | Entry:%.2f SL:%.2f Tgt:%.2f", name, dire, entry, sl, tgt)
            break

# MAIN LOOP
def main_loop():
    update_symbols_from_master()
    send_text("*v13 WARRIOR ONLINE*\nMonitoring Nifty, BankNifty and MCX. Ready.")
    log.info("Main loop started")
    last_scan_minute = -1
    while True:
        now  = datetime.now(IST)
        mins = now.hour * 60 + now.minute
        m    = now.minute
        in_window = (9 * 60 + 10) <= mins <= (23 * 60 + 30)
        if in_window:
            if state["daily_pnl"] >= SAFE_MODE_TRIGGER and not state["is_sureshot"]:
                state["is_sureshot"] = True
                send_text("SURESHOT MODE ON\nProfit target crossed. Tighter criteria active.")
                log.info("Switched to Sureshot mode")
            if state["active_trade"]:
                monitor_active_trade()
            elif not state["paused"] and m % 5 == 0 and m != last_scan_minute:
                last_scan_minute = m
                run_scanner()
        time.sleep(20)


if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  Dhan Warrior Bot  -  v13")
    log.info("=" * 50)
    log.info("DHAN_CLIENT_ID    : %s", "SET" if DHAN_CLIENT_ID else "MISSING")
    log.info("DHAN_ACCESS_TOKEN : %s", ("SET (len=" + str(len(DHAN_ACCESS_TOKEN)) + ")") if DHAN_ACCESS_TOKEN else "MISSING")
    threading.Thread(target=bot_listener, daemon=True).start()
    main_loop()

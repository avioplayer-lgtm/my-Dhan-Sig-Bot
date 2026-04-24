import os
import time
import logging
import threading
import requests
import pandas as pd
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
CHAT_ID           = os.environ.get("CHAT_ID")
DHAN_CLIENT_ID    = (os.environ.get("DHAN_CLIENT_ID") or "").strip()
DHAN_ACCESS_TOKEN = (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()

IST               = pytz.timezone("Asia/Kolkata")
SAFE_MODE_TRIGGER = 4000.0
MAX_DAILY_LOSS    = 1500.0

SYMBOLS = {
    "NIFTY":      {"interval": 50,   "lot": 65,   "dhan_scrip": "13", "ws_scrip": 13,  "segment": "IDX_I",    "inst": "INDEX"},
    "BANKNIFTY":  {"interval": 100,  "lot": 30,   "dhan_scrip": "25", "ws_scrip": 25,  "segment": "IDX_I",    "inst": "INDEX"},
    "CRUDEOIL":   {"interval": 100,  "lot": 100,  "dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
    "NATURALGAS": {"interval": 5,    "lot": 1250, "dhan_scrip": None, "ws_scrip": None, "segment": "MCX_COMM", "inst": "FUTCOM"},
}

DHAN_HEADERS = {
    "access-token": DHAN_ACCESS_TOKEN,
    "client-id":    DHAN_CLIENT_ID,
    "Content-Type": "application/json",
}

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_FILE_PATH  = "dhan_scrip_master.csv"

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("RailwayV10")

# ─────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────
state = {
    "daily_pnl":         0.0,
    "is_sureshot":       False,
    "active_trade":      None,
    "last_update_id":    0,
    "breakeven_alerted": False,
    "paused":            False,
}

# ─────────────────────────────────────────
# SCRIP MASTER
# ─────────────────────────────────────────
def update_symbols_from_master():
    log.info("Refreshing Scrip Master for active contracts...")
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
                    log.info(f"Set {name} -> {cfg['dhan_scrip']} ({active['SEM_TRADING_SYMBOL']})")
    except Exception as e:
        log.error(f"Master update failed: {e}")

# ─────────────────────────────────────────
# DHAN DATA
# ─────────────────────────────────────────
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
        log.error(f"get_ltp {name}: {e}")
        return None

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
        log.error(f"get_data {name}: {e}")
        return None

# ─────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────
def send_text(txt):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": txt, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.error(f"send_text error: {e}")

def bot_listener():
    log.info("Telegram command listener started.")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": state["last_update_id"] + 1, "timeout": 25},
                timeout=30,
            ).json()
            for up in r.get("result", []):
                state["last_update_id"] = up["update_id"]
                msg = up.get("message", {}).get("text", "")
                if "/status" in msg:
                    mode = "SURESHOT" if state["is_sureshot"] else "NORMAL"
                    at   = state["active_trade"]["symbol"] if state["active_trade"] else "None"
                    send_text(
                        f"*SYSTEM STATUS*\n"
                        f"Mode       : {mode}\n"
                        f"Daily P&L  : Rs.{state['daily_pnl']:.0f}\n"
                        f"Active     : {at}\n"
                        f"Paused     : {state['paused']}"
                    )
                elif "/setpnl" in msg:
                    try:
                        val = float(msg.split(" ")[1])
                        state["daily_pnl"]   = val
                        state["is_sureshot"] = val >= SAFE_MODE_TRIGGER
                        send_text(f"P&L set to Rs.{val:.0f}. Mode: {'Sureshot' if state['is_sureshot'] else 'Normal'}")
                    except Exception:
                        send_text("Format: /setpnl 2500")
                elif "/pause" in msg:
                    state["paused"] = True
                    send_text("Bot scanning PAUSED.")
                elif "/resume" in msg:
                    state["paused"] = False
                    send_text("Bot scanning RESUMED.")
                elif "/exited" in msg:
                    state["active_trade"]      = None
                    state["breakeven_alerted"] = False
                    send_text("Active trade cleared. Ready for next signal.")
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.error(f"bot_listener error: {e}")
            time.sleep(5)

# ─────────────────────────────────────────
# SCANNER
# ─────────────────────────────────────────
def run_scanner():
    for name, cfg in SYMBOLS.items():
        if cfg["dhan_scrip"] is None:
            continue
        df = get_data(name)
        if df is None or len(df) < 21:
            continue
        last = df.iloc[-1]
        dire = None
        if last["ema9"] > last["ema21"] and last["rsi"] > 55 and last["Volume"] > df["Volume"].tail(5).mean():
            dire = "CE"
        elif last["ema9"] < last["ema21"] and last["rsi"] < 45 and last["Volume"] > df["Volume"].tail(5).mean():
            dire = "PE"
        if dire:
            entry = last["Close"]
            sl    = entry - last["atr"] * 0.8 if dire == "CE" else entry + last["atr"] * 0.8
            tgt   = entry + last["atr"] * 1.5 if dire == "CE" else entry - last["atr"] * 1.5
            state["active_trade"]      = {"symbol": name, "direction": dire, "entry": entry, "sl": sl, "tgt": tgt, "atr": last["atr"]}
            state["breakeven_alerted"] = False
            mode_tag = "Sureshot" if state["is_sureshot"] else "Normal"
            send_text(
                f"*SIGNAL: {name} {dire}*\n"
                f"Entry  : {entry:.2f}\n"
                f"Target : {tgt:.2f}\n"
                f"SL     : {sl:.2f}\n"
                f"Mode   : {mode_tag}"
            )
            log.info(f"Signal: {name} {dire} | Entry:{entry:.2f} SL:{sl:.2f} Tgt:{tgt:.2f}")
            break  # one trade at a time

def monitor_active_trade():
    t    = state["active_trade"]
    live = get_ltp(t["symbol"])
    if not live:
        return
    move = (live - t["entry"]) if t["direction"] == "CE" else (t["entry"] - live)
    if not state["breakeven_alerted"] and move >= t["atr"] * 0.4:
        state["breakeven_alerted"] = True
        send_text(f"SAFE PLAY\n{t['symbol']} in profit. Move SL to cost.")
    hit_sl  = (live <= t["sl"])  if t["direction"] == "CE" else (live >= t["sl"])
    hit_tgt = (live >= t["tgt"]) if t["direction"] == "CE" else (live <= t["tgt"])
    if hit_sl or hit_tgt:
        status = "TARGET HIT" if hit_tgt else "SL/COST HIT"
        send_text(f"*{status}*\n{t['symbol']} at {live:.2f}.\nSend /exited to reset bot.")
        log.info(f"{status}: {t['symbol']} at {live:.2f}")
        state["active_trade"]      = None
        state["breakeven_alerted"] = False

# ─────────────────────────────────────────
# MAIN TRADING LOOP
# ─────────────────────────────────────────
def trading_cycle():
    update_symbols_from_master()
    send_text("*v10 SCANNER ACTIVE*\nMonitoring Nifty, BankNifty & MCX.")
    log.info("Trading cycle started")

    last_scan_minute = -1

    while True:
        now = datetime.now(IST)
        h   = now.hour
        m   = now.minute
        mins = h * 60 + m

        # Trading window: 9:15 AM (555) to 3:25 PM (925) for NSE
        # MCX: 9:00 AM to 11:30 PM — use broader window, symbols handle themselves
        in_window = (9 * 60 + 15) <= mins <= (23 * 60 + 30)

        if in_window:
            # Sureshot mode transition
            if state["daily_pnl"] >= SAFE_MODE_TRIGGER and not state["is_sureshot"]:
                state["is_sureshot"] = True
                send_text("SWITCHING TO SURESHOT MODE\nCriteria: EMA Cross + RSI (55-70/30-45) + Volume Confirmation.")
                log.info("Switched to Sureshot mode")

            # Monitor active trade every loop (every 20s)
            if state["active_trade"]:
                monitor_active_trade()

            # Scan every 5 minutes (track by minute, not by second window)
            elif not state["paused"] and m % 5 == 0 and m != last_scan_minute:
                last_scan_minute = m
                log.info(f"Running scanner at {now.strftime('%H:%M')}")
                run_scanner()

        time.sleep(20)


if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  Dhan Signal Bot  -  v10")
    log.info("=" * 50)
    log.info(f"DHAN_CLIENT_ID    : {'SET' if DHAN_CLIENT_ID else 'MISSING'}")
    log.info(f"DHAN_ACCESS_TOKEN : {'SET (len=' + str(len(DHAN_ACCESS_TOKEN)) + ')' if DHAN_ACCESS_TOKEN else 'MISSING'}")
    threading.Thread(target=bot_listener, daemon=True).start()
    trading_cycle()

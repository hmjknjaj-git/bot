import csv
import hashlib
import hmac
import json
import os
import time
import requests

# ==========================================
# CONFIGURATION & PARAMETERS
# ==========================================
BASE = "https://api.india.delta.exchange"
SYMBOL, RES = "ETHUSD", "5m"
FAST, SLOW = 9, 21
GAP_PCT = 0.10         # EMAs must differ by this % before a signal counts
STOP_PCT = 0.7         # Stop loss threshold (% from entry price)
COOLDOWN_CANDLES = 3   # Wait this many candles after closing a trade before re-entering
LOTS = 1               # HARD CAP. 1 lot = 0.01 ETH
LOT_ETH = 0.01
KILL_USD = 3.0         # Stop opening trades if cumulative losses exceed this USD amount

MODE = os.environ.get("MODE", "paper")
KEY = os.environ.get("DELTA_API_KEY", "")
SECRET = os.environ.get("DELTA_API_SECRET", "")
STATE, LOG = "state.json", "trades.csv"


# ==========================================
# API HELPER FUNCTIONS
# ==========================================
def get(path, params=None):
    r = requests.get(BASE + path, params=params, timeout=20)
    if not r.ok:
        raise RuntimeError(f"{path}: {r.status_code} {r.text}")
    return r.json()


def api(method, path, params=None, body=None):
    ts = str(int(time.time()))
    query = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    payload = json.dumps(body) if body else ""
    sig = hmac.new(
        SECRET.encode(),
        (method + ts + path + query + payload).encode(),
        hashlib.sha256
    ).hexdigest()
    
    h = {
        "api-key": KEY, 
        "timestamp": ts, 
        "signature": sig,
        "User-Agent": "python-bot", 
        "Content-Type": "application/json"
    }
    r = requests.request(
        method, BASE + path + query, headers=h,
        data=payload or None, timeout=20
    )
    if not r.ok:
        raise RuntimeError(f"{path}: {r.status_code} {r.text}")
    return r.json()


# ==========================================
# STRATEGY & TECHNICAL INDICATORS
# ==========================================
def candles():
    end = int(time.time())
    j = get("/v2/history/candles", {
        "symbol": SYMBOL, "resolution": RES,
        "start": end - 300 * 200, "end": end
    })
    rows = sorted(j["result"], key=lambda c: c["time"])
    return [(c["time"], float(c["close"])) for c in rows[:-1]]  # drop forming candle


def ema(values, n):
    k, e = 2 / (n + 1), values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {
        "pos": 0, "entry": 0.0, "total_usd": 0.0, "trades": 0,
        "last_close_t": 0, "blocked": 0
    }


def log_trade(row):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "mode", "action", "price", "trade_usd", "total_usd"])
        w.writerow(row)


def order(pid, side, reduce):
    body = {
        "product_id": pid, "size": LOTS, 
        "side": side, "order_type": "market_order"
    }
    if reduce:
        body["reduce_only"] = "true"
    return api("POST", "/v2/orders", body=body)


# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
def main():
    rows = candles()
    if len(rows) < SLOW + 5:
        print("not enough data")
        return
        
    t, price = rows[-1]
    cl = [c for _, c in rows]
    f, sl = ema(cl, FAST), ema(cl, SLOW)
    gap = (f - sl) / sl * 100
    s = load()
    pid = get(f"/v2/products/{SYMBOL}")["result"]["id"]

    if MODE in ("check", "live"):
        p = api("GET", "/v2/positions", {"product_id": pid}).get("result") or {}
        size = int(float(p.get("size") or 0))
        s["pos"] = (size > 0) - (size < 0)
        s["entry"] = float(p.get("entry_price") or 0) if size else 0.0
        
    if MODE == "check":
        bal = api("GET", "/v2/wallet/balances")["result"]
        print("keys OK | product_id", pid, "| position", s["pos"], "| price", price, "| ema gap %", round(gap, 3))
        print("balances:", [(b.get("asset_symbol"), b.get("available_balance")) for b in bal])
        return

    want = 1 if gap > GAP_PCT else -1 if gap < -GAP_PCT else s["pos"]
    now = time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))
    stopped = False

    if s["pos"] != 0:
        move = (price - s["entry"]) / s["entry"] * 100 * s["pos"]
        stopped = move <= -STOP_PCT
        if stopped or want != s["pos"]:
            if MODE == "live":
                order(pid, "sell" if s["pos"] == 1 else "buy", True)
            usd = (price - s["entry"]) * s["pos"] * LOTS * LOT_ETH
            s["total_usd"] += usd
            s["trades"] += 1
            name = "CLOSE_" + ("LONG" if s["pos"] == 1 else "SHORT") + ("_STOP" if stopped else "")
            log_trade([now, MODE, name, price, round(usd, 4), round(s["total_usd"], 4)])
            if stopped:
                s["blocked"] = s["pos"]
            s["pos"], s["last_close_t"] = 0, t

    if s["blocked"] and want != s["blocked"]:
        s["blocked"] = 0
        
    can_open = (
        s["pos"] == 0 and want != 0 and want != s["blocked"]
        and t - s["last_close_t"] >= COOLDOWN_CANDLES * 300
        and s["total_usd"] > -KILL_USD
    )
    
    if can_open:
        if MODE == "live":
            order(pid, "buy" if want == 1 else "sell", False)
        s["pos"], s["entry"] = want, price
        log_trade([now, MODE, "OPEN_" + ("LONG" if want == 1 else "SHORT"), price, "", round(s["total_usd"], 4)])

    json.dump(s, open(STATE, "w"))
    print(now, MODE, "price", price, "pos", s["pos"], "total_usd", round(s["total_usd"], 4))

    # ==========================================
    # GIT AUTO-SYNC
    # ==========================================
    print("Syncing data files back to repository...")
    os.system("git config --global user.name 'bot'")
    os.system("git config --global user.email 'bot@users.noreply.github.com'")
    os.system("git pull origin main --rebase")
    os.system("git add state.json trades.csv")
    os.system('git commit -m "update bot state"')
    os.system("git push origin main")
    print("Execution and sync finished successfully.")


if __name__ == "__main__":
    main()

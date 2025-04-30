"""
pumpfunbot/strategy.py
Logique trading + indicateurs + IA + risk‐manager
"""

import time, collections, pathlib, joblib, json
from dataclasses import dataclass, field
from pf_helpers   import abbreviate, sol_to_eur
from param_store  import PARAMS
from data_logger  import append as log_row
from notifications import send

# ───────────────────────────────────────────────────────────────
TOTAL_SUPPLY  = 69_000                       # seuil graduation pump.fun
MODEL_PATH    = pathlib.Path("model.joblib")
clf = joblib.load(MODEL_PATH) if MODEL_PATH.exists() else None

# ───────────────────────────────────────────────────────────────
# Structures
# ───────────────────────────────────────────────────────────────
@dataclass
class TokenInfo:
    mint: str
    name: str
    progress: float   = 0.0        # 0–1
    mcap_sol: float   = 0.0
    holders:  int     = 0
    devPct:   float   = 0.0
    lpSize:   float   = 0.0
    hist_mcap: collections.deque = field(default_factory=lambda: collections.deque(maxlen=900))  # (ts, mcap_sol)
    vol_hist:  collections.deque = field(default_factory=lambda: collections.deque(maxlen=900))  # (ts, dvol)

tokens:       dict[str,TokenInfo] = {}
open_trades:  dict[str,dict]      = {}
trades:       list[dict]          = []

balance      = 0.0
equity_high  = 0.0
session_dd   = 0.0                # drawdown € de la session

# ───────────────────────────────────────────────────────────────
# Indicateurs rapides
# ───────────────────────────────────────────────────────────────
def _prices(t: TokenInfo) -> list[float]:
    """« Prix par token » ≈ mcap / sold (approx.)"""
    if not t.hist_mcap: return []
    sold = max(t.progress * TOTAL_SUPPLY, 1)          # éviter 0
    return [mc / sold for _, mc in t.hist_mcap]

def sma(lst, n):  return None if len(lst) < n else sum(lst[-n:]) / n
def rsi(lst, n=14):
    if len(lst) < n+1: return None
    gain = [max(0,b-a) for a,b in zip(lst,lst[1:])]
    loss = [max(0,a-b) for a,b in zip(lst,lst[1:])]
    ag, al = sum(gain[-n:])/n, sum(loss[-n:])/n
    return 100 if al==0 else 100 - 100/(1+ag/al)
def atr(lst, n=14):
    if len(lst) < n+1: return None
    return sum(abs(b-a) for a,b in zip(lst,lst[1:])) / n
def vol_5m(t): return sum(v for _,v in list(t.vol_hist)[-300:])
def price_change_1m(t):
    pr = _prices(t)
    if len(pr) < 60: return 0.0
    return (pr[-1]-pr[-60]) / pr[-60]

__all__ = [
    "tokens","open_trades","trades","balance",
    "init_balance","on_pump",
    "price_change_1m","vol_5m"
]

# ───────────────────────────────────────────────────────────────
# Initialisation capital
# ───────────────────────────────────────────────────────────────
def init_balance(v: float):
    global balance, equity_high, session_dd
    balance = equity_high = v
    session_dd = 0.0

# ───────────────────────────────────────────────────────────────
# Position sizing (risk % / ATR)
# ───────────────────────────────────────────────────────────────
def _size(atr14: float) -> float:
    risk_eur = balance * (PARAMS["risk_pct"] / 100)
    if atr14 == 0: atr14 = 0.01        # fallback tiny ATR
    return risk_eur / atr14

# ───────────────────────────────────────────────────────────────
# Open / Close helpers
# ───────────────────────────────────────────────────────────────
def _open(t: TokenInfo, feats: dict):
    global balance
    atr14  = atr(_prices(t),14) or 0.01
    entry  = t.mcap_sol * sol_to_eur()    # on “achète” la mcap en € (symbolique)
    qty    = _size(atr14)                 # fictitious units
    pos = {
        "mint":  t.mint,
        "name":  t.name,
        "entry": entry,
        "qty":   qty,
        "tp":    entry + PARAMS["tp_mult"]*atr14,
        "stop":  entry - PARAMS["sl_mult"]*atr14,
        "trail": entry - PARAMS["sl_mult"]*atr14,
        "high":  entry,
        "invest": entry*qty,
        "features": feats,
        "time_in": time.time()
    }
    open_trades[t.mint] = pos
    balance -= pos["invest"]
    send("OPEN", f"{t.name} mcap€ {entry:.0f}")

def _close(pos:dict, price_eur:float, why:str):
    global balance, equity_high, session_dd
    eff = price_eur                          # pas de slippage sur mcap
    pnl = (eff - pos["entry"]) * pos["qty"]
    balance += pos["invest"] + pnl
    equity_high = max(equity_high, balance)
    session_dd  = min(session_dd, balance - equity_high)
    # log CSV
    log_row({**pos["features"]}, 1 if pnl>0 else 0)
    trades.append({**pos, "exit":eff, "pnl":pnl, "reason":why,
                   "time_out": time.time()})
    del open_trades[pos["mint"]]
    send("CLOSE", f"{pos['name']} {why} pnl {pnl:.2f}")
    # stop si max DD
    if abs(session_dd) > equity_high * (PARAMS["max_dd_pct"]/100):
        for m in list(open_trades):
            _close(open_trades[m], price_eur, "DD-cut")
        raise SystemExit("Max daily DD reached")

# ───────────────────────────────────────────────────────────────
# IA
# ───────────────────────────────────────────────────────────────
def _ia_ok(feats):
    if not (PARAMS["use_ai"] and clf): return True
    vec = [[feats[k] for k in
            ("progress","mcap_eur","devPct","lpSize","holders",
             "sma5","sma15","rsi14","atr14","vol_5m")]]
    proba = clf.predict_proba(vec)[0][1]
    send("AI", f"{feats['name']} proba {proba:.2f}")
    return proba >= PARAMS["ai_thresh"]

# ───────────────────────────────────────────────────────────────
# Feature builder
# ───────────────────────────────────────────────────────────────
def _compute_feats(t:TokenInfo, now:float):
    prices = _prices(t)
    mcap_eur = t.mcap_sol * sol_to_eur()
    return {
        "ts": now,
        "mint": t.mint,
        "name": t.name,
        "progress": t.progress,
        "mcap_eur": mcap_eur,
        "devPct": t.devPct,
        "lpSize": t.lpSize,
        "holders": t.holders,
        "sma5":  sma(prices,300) or 0,
        "sma15": sma(prices,900) or 0,
        "rsi14": rsi(prices,14)  or 0,
        "atr14": atr(prices,14)  or 0,
        "vol_5m": vol_5m(t)
    }

# ───────────────────────────────────────────────────────────────
# WebSocket handler
# ───────────────────────────────────────────────────────────────
async def on_pump(msg:dict, ws):
    tx  = msg.get("txType")
    mint= msg.get("mint")

    # ----- création ---------------------------------------------------------
    if tx == "create" and mint:
        tokens[mint] = TokenInfo(mint, abbreviate(mint))
        await ws.send(json.dumps({
            "method": "subscribeTokenTrades",   # <— correct plural
            "keys":   [mint]
        }))
        return

    # ----- trades -----------------------------------------------------------
    if tx in ("buy","sell") and mint in tokens:
        t = tokens[mint]
        t.progress = msg.get("progressPercent", t.progress*100)/100
        t.mcap_sol = msg.get("marketCapSol",   t.mcap_sol)
        t.holders  = msg.get("holderCount",    t.holders)
        t.devPct   = msg.get("devPercent",     t.devPct)
        t.lpSize   = msg.get("lpLamports",     t.lpSize) / 1e9

        dvol       = msg.get("marketCapSol", 0.0)
        now        = time.time()
        t.hist_mcap.append((now, t.mcap_sol))
        t.vol_hist.append((now, dvol))

        # --- positions déjà ouvertes
        if mint in open_trades:
            pos = open_trades[mint]
            if t.mcap_sol*sol_to_eur() > pos["high"]:
                pos["high"] = t.mcap_sol*sol_to_eur()
                pos["trail"]= pos["high"] - PARAMS["sl_mult"]*(atr(_prices(t),14) or 0)
            price_eur = t.mcap_sol * sol_to_eur()
            if price_eur >= pos["tp"]:
                _close(pos, price_eur, "TP")
            elif price_eur <= pos["trail"] or price_eur <= pos["stop"]:
                _close(pos, price_eur, "SL/Trail")

        # --- nouvelle entrée potentielle
        else:
            if t.progress >= 0.90:
                feats = _compute_feats(t, now)
                if _ia_ok(feats):
                    _open(t, feats)
        return

    # ----- migration --------------------------------------------------------
    if tx == "migrate" and mint in open_trades:
        mcap_eur = tokens[mint].mcap_sol * sol_to_eur()
        _close(open_trades[mint], mcap_eur, "GRAD")

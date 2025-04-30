# pumpfunbot/strategy.py
import time, collections, pathlib, joblib, json
from dataclasses import dataclass, field
from pf_helpers  import compute_price_from_pool, abbreviate
from param_store  import PARAMS
from data_logger  import append as log_row
from notifications import send

TOTAL_SUPPLY = 69_000
MODEL_PATH   = pathlib.Path("model.joblib")
clf = joblib.load(MODEL_PATH) if MODEL_PATH.exists() else None

# ───────────────────────────────────────────────
# Structures
# ───────────────────────────────────────────────
@dataclass
class TokenInfo:
    mint: str
    name: str
    sold: float = 0.0
    progress: float = 0.0
    price: float = 0.0
    devPct: float = 0.0
    lpSize: float = 0.0
    holders: int  = 0
    hist: collections.deque = field(default_factory=lambda: collections.deque(maxlen=900))
    vol_hist: collections.deque = field(default_factory=lambda: collections.deque(maxlen=900))

tokens, open_trades, trades = {}, {}, []
balance = equity_high = 0.0
session_dd = 0.0

# ───────────────────────────────────────────────
# Helper indicateurs
# ───────────────────────────────────────────────
def _prices(t): return [p for _, p in t.hist]
def sma(pr, n): return None if len(pr) < n else sum(pr[-n:]) / n
def rsi(pr, n=14):
    if len(pr) < n + 1:
        return None
    gains = [max(0, b - a) for a, b in zip(pr, pr[1:])]
    losses = [max(0, a - b) for a, b in zip(pr, pr[1:])]
    ag, al = sum(gains[-n:]) / n, sum(losses[-n:]) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)
def atr(pr, n=14):
    if len(pr) < n + 1:
        return None
    return sum(abs(b - a) for a, b in zip(pr, pr[1:])) / n
def vol_5m(t): return sum(v for _, v in list(t.vol_hist)[-300:])
def price_change_1m(t):
    pr = _prices(t)
    if len(pr) < 60:
        return 0.0
    return (pr[-1] - pr[-60]) / pr[-60]

__all__ = [
    "tokens", "open_trades", "trades", "balance",
    "init_balance", "on_pump",
    "price_change_1m", "vol_5m"
]

# ───────────────────────────────────────────────
# Risk manager
# ───────────────────────────────────────────────
def init_balance(v: float):
    global balance, equity_high, session_dd
    balance = equity_high = v
    session_dd = 0

def _size(atr14: float) -> float:
    risk_eur = balance * (PARAMS["risk_pct"] / 100)
    if atr14 == 0:
        atr14 = 0.01
    return risk_eur / atr14

# ───────────────────────────────────────────────
# Ouverture / fermeture
# ───────────────────────────────────────────────
def _open(t, feats):
    global balance
    atr14 = atr(_prices(t), 14) or 0.01
    entry = t.price * (1 + PARAMS["slippage"] / 100)
    qty   = _size(atr14)
    pos = {
        "mint": t.mint, "name": t.name,
        "entry": entry, "qty": qty,
        "tp": entry + PARAMS["tp_mult"] * atr14,
        "stop": entry - PARAMS["sl_mult"] * atr14,
        "trail": entry - PARAMS["sl_mult"] * atr14,
        "high": entry,
        "invest": entry * qty,
        "features": feats,
        "time_in": time.time()
    }
    open_trades[t.mint] = pos
    balance -= pos["invest"]
    send("OPEN", f"{t.name} @ {entry:.4f}")

def _close(pos, px, why):
    global balance, equity_high, session_dd
    eff = px * (1 - PARAMS["slippage"] / 100)
    pnl = (eff - pos["entry"]) * pos["qty"]
    balance += pos["invest"] + pnl
    equity_high = max(equity_high, balance)
    session_dd = min(session_dd, balance - equity_high)
    log_row(pos["features"], 1 if pnl > 0 else 0)
    trades.append({**pos, "exit": eff, "pnl": pnl,
                   "pct": pnl / pos["invest"] * 100,
                   "reason": why, "time_out": time.time()})
    del open_trades[pos["mint"]]
    send("CLOSE", f"{pos['name']} {why} pnl {pnl:.2f}")
    # coupe si drawdown > limite
    if abs(session_dd) > equity_high * (PARAMS["max_dd_pct"] / 100):
        for m in list(open_trades):
            _close(open_trades[m], tokens[m].price, "DD-cut")
        raise SystemExit("Max daily DD reached")

# ───────────────────────────────────────────────
# IA
# ───────────────────────────────────────────────
def _compute_feats(t, now):
    pr = _prices(t)
    return {
        "ts": now, "mint": t.mint, "name": t.name,
        "progress": t.progress, "price": t.price,
        "devPct": t.devPct, "lpSize": t.lpSize, "holders": t.holders,
        "sma5": sma(pr, 300) or 0, "sma15": sma(pr, 900) or 0,
        "rsi14": rsi(pr, 14) or 0, "atr14": atr(pr, 14) or 0,
        "vol_5m": vol_5m(t)
    }

def _ia_ok(feats):
    if not (PARAMS["use_ai"] and clf):
        return True
    vector = [[feats[k] for k in ("progress","price","devPct","lpSize",
                                  "holders","sma5","sma15","rsi14",
                                  "atr14","vol_5m")]]
    proba = clf.predict_proba(vector)[0][1]
    send("AI", f"{feats['name']} proba={proba:.2f}")
    return proba >= PARAMS["ai_thresh"]

# ───────────────────────────────────────────────
# Handler WebSocket (appelé par fetcher)
# ───────────────────────────────────────────────
async def on_pump(msg, ws):
    tx, mint = msg.get("txType"), msg.get("mint")

    # création d'un token
    if tx == "create" and mint:
        tokens[mint] = TokenInfo(mint, abbreviate(mint))
        # PATCH: subscribeTokenTrades (pluriel)
        await ws.send(json.dumps({
            "method": "subscribeTokenTrades",
            "keys": [mint]
        }))
        return

    # mise à jour trading
    if tx in ("buy", "sell") and mint in tokens:
        t = tokens[mint]
        vs, vt = msg.get("vSolInBondingCurve"), msg.get("vTokensInBondingCurve")
        if vs is not None and vt is not None:
            t.sold = TOTAL_SUPPLY - vt
            t.progress = t.sold / TOTAL_SUPPLY
            t.price = compute_price_from_pool(vs, vt)
        t.devPct  = msg.get("devPercent", 0)
        t.lpSize  = msg.get("lpLamports", 0) / 1e9
        t.holders = msg.get("holderCount", 0)
        dvol      = msg.get("marketCapSol", 0.0)
        now = time.time()
        t.hist.append((now, t.price))
        t.vol_hist.append((now, dvol))

        # suivi position
        if mint in open_trades:
            pos = open_trades[mint]
            if t.price > pos["high"]:
                pos["high"] = t.price
                pos["trail"] = max(pos["trail"],
                                   t.price - PARAMS["sl_mult"] * (atr(_prices(t), 14) or 0))
            if t.price >= pos["tp"]:
                _close(pos, t.price, "TP")
            elif t.price <= pos["trail"] or t.price <= pos["stop"]:
                _close(pos, t.price, "SL/Trail")
        else:
            if t.progress >= 0.90:
                feats = _compute_feats(t, now)
                if _ia_ok(feats):
                    _open(t, feats)
        return

    # migration PumpSwap
    if tx == "migrate" and mint in open_trades:
        _close(open_trades[mint], tokens[mint].price, "GRAD")

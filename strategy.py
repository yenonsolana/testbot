# pumpfunbot/strategy.py
import time, collections, pathlib, json, joblib
from dataclasses import dataclass, field
from pf_helpers import compute_price_from_pool, abbreviate
from param_store  import PARAMS
from data_logger  import append as log_row
from notifications import send

# ───────────────────────────────────────────────────────────────
# constantes globales
# ───────────────────────────────────────────────────────────────
TOTAL_SUPPLY = 69_000                   # graduation Pump.fun
MODEL_PATH   = pathlib.Path("model.joblib")
clf = joblib.load(MODEL_PATH) if MODEL_PATH.exists() else None

# ───────────────────────────────────────────────────────────────
# structures de données
# ───────────────────────────────────────────────────────────────
@dataclass
class TokenInfo:
    mint: str
    name: str
    sold: float = 0.0
    progress: float = 0.0
    price: float = 0.0
    devPct: float = 0.0         # % des devs
    lpSize: float  = 0.0        # valeur LP (SOL)
    holders: int   = 0
    hist: collections.deque     = field(default_factory=lambda: collections.deque(maxlen=900))   # (ts,price) 15 min
    vol_hist: collections.deque = field(default_factory=lambda: collections.deque(maxlen=900))   # (ts,vol)   15 min

tokens:      dict[str,TokenInfo] = {}
open_trades: dict[str,dict]      = {}
trades:      list[dict]          = []

balance: float     = 0.0          # capital disponible
equity_high: float = 0.0          # plus-haut depuis minuit
session_dd: float  = 0.0          # draw-down absolu € depuis le peak

# ───────────────────────────────────────────────────────────────
# indicateurs rapides
# ───────────────────────────────────────────────────────────────
def _prices(t: TokenInfo) -> list[float]:
    return [p for _, p in t.hist]

def sma(values: list[float], n: int):
    if len(values) < n: return None
    return sum(values[-n:]) / n

def rsi(values: list[float], n: int = 14):
    if len(values) < n + 1: return None
    gains = [max(0, b - a) for a, b in zip(values, values[1:])]
    losses= [max(0, a - b) for a, b in zip(values, values[1:])]
    ag = sum(gains[-n:]) / n
    al = sum(losses[-n:]) / n
    if al == 0: return 100
    rs = ag / al
    return 100 - 100 / (1 + rs)

def atr(values: list[float], n: int = 14):
    if len(values) < n + 1: return None
    trs = [abs(b - a) for a, b in zip(values, values[1:])]
    return sum(trs[-n:]) / n

def vol_5m(t: TokenInfo) -> float:
    """volume cumulé (SOL) sur les 5 dernières minutes (300 ticks)."""
    return sum(v for _, v in list(t.vol_hist)[-300:])

def price_change_1m(t: TokenInfo) -> float:
    """variation prix sur 60 ticks (≈ 1 minute)."""
    pr = _prices(t)
    if len(pr) < 60: return 0.0
    return (pr[-1] - pr[-60]) / pr[-60]

# ───────────────────────────────────────────────────────────────
# initialisation du portefeuille
# ───────────────────────────────────────────────────────────────
def init_balance(v: float):
    global balance, equity_high, session_dd
    balance = equity_high = v
    session_dd = 0.0

# ───────────────────────────────────────────────────────────────
# calcul taille de position (= risk € / ATR)
# ───────────────────────────────────────────────────────────────
def _position_size(atr14: float) -> float:
    risk_eur = balance * (PARAMS["risk_pct"] / 100)      # € à risquer
    if atr14 == 0:
        atr14 = 0.01                                     # plancher
    qty = risk_eur / atr14
    return qty

# ───────────────────────────────────────────────────────────────
# open / close
# ───────────────────────────────────────────────────────────────
def _open(t: TokenInfo, feats: dict):
    global balance
    atr14 = atr(_prices(t), 14) or 0.01
    entry = t.price * (1 + PARAMS["slippage"] / 100)
    qty   = _position_size(atr14)
    invest = entry * qty
    pos = {
        "mint"   : t.mint,
        "name"   : t.name,
        "entry"  : entry,
        "qty"    : qty,
        "invest" : invest,
        "tp"     : entry + PARAMS["tp_mult"] * atr14,
        "stop"   : entry - PARAMS["sl_mult"] * atr14,
        "trail"  : entry - PARAMS["sl_mult"] * atr14,
        "high"   : entry,
        "time_in": time.time(),
        "features": feats
    }
    open_trades[t.mint] = pos
    balance -= invest
    send("OPEN", f"{t.name} @ {entry:.4f}")

def _close(pos: dict, px: float, reason: str):
    global balance, equity_high, session_dd
    eff = px * (1 - PARAMS["slippage"] / 100)
    pnl = (eff - pos["entry"]) * pos["qty"]
    balance += pos["invest"] + pnl
    equity_high = max(equity_high, balance)
    session_dd  = min(session_dd, balance - equity_high)

    # label pour le dataset
    label = 1 if pnl > 0 else 0
    log_row({**pos["features"]}, label)

    trades.append({**pos, "exit": eff, "pnl": pnl,
                   "pct": pnl / pos["invest"] * 100,
                   "reason": reason,
                   "time_out": time.time()})
    del open_trades[pos["mint"]]
    send("CLOSE", f"{pos['name']} {reason} pnl {pnl:.2f}")

    # stop max draw-down journalier
    if abs(session_dd) > equity_high * (PARAMS["max_dd_pct"] / 100):
        for m in list(open_trades):
            _close(open_trades[m], tokens[m].price, "DD-cut")
        raise SystemExit("Max daily DD reached ❌")

# ───────────────────────────────────────────────────────────────
# IA : features + décision
# ───────────────────────────────────────────────────────────────
def _build_features(t: TokenInfo, ts: float) -> dict:
    pr = _prices(t)
    return {
        "ts": ts,
        "mint": t.mint,
        "name": t.name,
        "progress": t.progress,
        "price": t.price,
        "devPct": t.devPct,
        "lpSize": t.lpSize,
        "holders": t.holders,
        "sma5":  sma(pr, 300) or 0,
        "sma15": sma(pr, 900) or 0,
        "rsi14": rsi(pr, 14) or 0,
        "atr14": atr(pr, 14) or 0,
        "vol_5m": vol_5m(t)
    }

def _ia_accept(feats: dict) -> bool:
    if not (PARAMS["use_ai"] and clf):
        return True
    vec = [[feats[k] for k in
            ("progress", "price", "devPct", "lpSize", "holders",
             "sma5", "sma15", "rsi14", "atr14", "vol_5m")]]
    proba = clf.predict_proba(vec)[0][1]
    send("AI", f"{feats['name']} proba={proba:.2f}")
    return proba >= PARAMS["ai_thresh"]

# ───────────────────────────────────────────────────────────────
# handler WebSocket principal
# ───────────────────────────────────────────────────────────────
async def on_pump(msg: dict, ws):
    tx, mint = msg.get("txType"), msg.get("mint")

    # — création —
    if tx == "create" and mint:
        tokens[mint] = TokenInfo(mint, abbreviate(mint))
        await ws.send(json.dumps({"method": "subscribeTokenTrade",
                                  "keys": [mint]}))
        return

    # — trade —
    if tx in ("buy", "sell") and mint in tokens:
        t = tokens[mint]

        # update fondamentaux
        vs = msg.get("vSolInBondingCurve")
        vt = msg.get("vTokensInBondingCurve")
        if vs is not None and vt is not None:
            t.sold      = TOTAL_SUPPLY - vt
            t.progress  = t.sold / TOTAL_SUPPLY
            t.price     = compute_price_from_pool(vs, vt)

        t.devPct  = msg.get("devPercent",   t.devPct)
        t.lpSize  = msg.get("lpLamports",   t.lpSize * 1e9) / 1e9
        t.holders = msg.get("holderCount",  t.holders)

        dvol = msg.get("marketCapSol", 0.0)
        now  = time.time()
        t.hist.append((now, t.price))
        t.vol_hist.append((now, dvol))

        # gestion position ouverte
        if mint in open_trades:
            pos = open_trades[mint]
            if t.price > pos["high"]:
                pos["high"] = t.price
                pos["trail"] = max(
                    pos["trail"],
                    t.price - PARAMS["sl_mult"] * (atr(_prices(t), 14) or 0)
                )
            if t.price >= pos["tp"]:
                _close(pos, t.price, "TP")
            elif t.price <= pos["trail"] or t.price <= pos["stop"]:
                _close(pos, t.price, "SL/Trail")
        else:
            # conditions d'entrée
            if t.progress >= 0.90:
                feats = _build_features(t, now)
                if _ia_accept(feats):
                    _open(t, feats)
        return

    # — migration —
    if tx == "migrate" and mint in open_trades:
        _close(open_trades[mint], tokens[mint].price, "GRAD")

# ───────────────────────────────────────────────────────────────
# API publique pour l’interface / autres modules
# ───────────────────────────────────────────────────────────────
__all__ = [
    "tokens", "open_trades", "trades",
    "balance", "init_balance", "on_pump",
    "price_change_1m", "vol_5m"
]

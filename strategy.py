"""
pumpfunbot/strategy.py
Trading logic + indicateurs + IA + risk-manager (market-cap)
"""

import time, collections, pathlib, joblib, json
from dataclasses import dataclass, field
from pf_helpers   import abbreviate, sol_to_eur
from param_store  import PARAMS
from data_logger  import append as log_row
from notifications import send

# ────────────────────────── constantes ─────────────────────────
TOKEN_SUPPLY      = 1_000_000_000          # supply fixe Pump.fun
MIGRATE_CAP_EUR   = 69_000                 # seuil migration
MODEL_PATH        = pathlib.Path("model.joblib")
clf = joblib.load(MODEL_PATH) if MODEL_PATH.exists() else None

# ────────────────────────── structures ─────────────────────────
@dataclass
class TokenInfo:
    mint: str
    name: str
    progress: float = 0.0      # 0-1 basé sur mcap / 69 k€
    mcap_sol: float = 0.0
    holders:  int   = 0
    devPct:   float = 0.0
    lpSize:   float = 0.0
    hist_mcap: collections.deque = field(default_factory=lambda: collections.deque(maxlen=900))
    vol_hist:  collections.deque = field(default_factory=lambda: collections.deque(maxlen=900))

tokens: dict[str,TokenInfo] = {}
open_trades: dict[str,dict] = {}
trades: list[dict]          = []

balance = equity_high = session_dd = 0.0

# ──────────────────────── indicateurs rapides ──────────────────
def _prices(t: TokenInfo):
    """Approx price = mcap / supply (en €)."""
    if not t.hist_mcap: return []
    return [mc*sol_to_eur()/TOKEN_SUPPLY for _,mc in t.hist_mcap]

def sma(lst,n):  return None if len(lst)<n else sum(lst[-n:])/n
def rsi(lst,n=14):
    if len(lst)<n+1: return None
    g=[max(0,b-a) for a,b in zip(lst,lst[1:])]
    l=[max(0,a-b) for a,b in zip(lst,lst[1:])]
    ag,al=sum(g[-n:])/n, sum(l[-n:])/n
    return 100 if al==0 else 100-100/(1+ag/al)
def atr(lst,n=14):
    if len(lst)<n+1: return None
    return sum(abs(b-a) for a,b in zip(lst,lst[1:]))/n
def vol_5m(t): return sum(v for _,v in list(t.vol_hist)[-300:])
def price_change_1m(t):
    pr=_prices(t)
    return 0 if len(pr)<60 else (pr[-1]-pr[-60])/pr[-60]

__all__ = ["tokens","open_trades","trades","balance",
           "init_balance","on_pump","price_change_1m","vol_5m"]

# ──────────────────────── capital & sizing ─────────────────────
def init_balance(v:float):
    global balance,equity_high,session_dd
    balance=equity_high=v; session_dd=0.0

def _size(atr14:float):
    risk_eur = balance * (PARAMS["risk_pct"]/100)
    return risk_eur / (atr14 or 0.01)

# ───────────────────────── open / close ────────────────────────
def _open(t, feats):
    global balance
    atr14 = atr(_prices(t),14) or 0.01
    entry = t.mcap_sol * sol_to_eur()
    qty   = _size(atr14)
    pos = {
        "mint":t.mint,"name":t.name,"entry":entry,"qty":qty,
        "tp":entry+PARAMS["tp_mult"]*atr14,
        "stop":entry-PARAMS["sl_mult"]*atr14,
        "trail":entry-PARAMS["sl_mult"]*atr14,
        "high":entry,"invest":entry*qty,
        "features":feats,"time_in":time.time()
    }
    open_trades[t.mint]=pos
    balance-=pos["invest"]
    send("OPEN",f"{t.name} mcap€ {entry:.0f}")

def _close(pos, px, why):
    global balance,equity_high,session_dd
    pnl=(px-pos["entry"])*pos["qty"]
    balance+=pos["invest"]+pnl
    equity_high=max(equity_high,balance)
    session_dd=min(session_dd,balance-equity_high)
    log_row({**pos["features"]}, 1 if pnl>0 else 0)
    trades.append({**pos,"exit":px,"pnl":pnl,"reason":why,"time_out":time.time()})
    del open_trades[pos["mint"]]
    send("CLOSE",f"{pos['name']} {why} pnl {pnl:.2f}")

# ──────────────────────── IA gate ──────────────────────────────
def _ia_ok(feats):
    if not (PARAMS["use_ai"] and clf): return True
    vec=[[feats[k] for k in ("progress","mcap_eur","devPct","lpSize","holders",
                             "sma5","sma15","rsi14","atr14","vol_5m")]]
    return clf.predict_proba(vec)[0][1] >= PARAMS["ai_thresh"]

# ─────────────────────── feature builder ───────────────────────
def _feats(t,ts):
    pr=_prices(t); mcap_eur=t.mcap_sol*sol_to_eur()
    return {
        "ts":ts,"mint":t.mint,"name":t.name,"progress":t.progress,
        "mcap_eur":mcap_eur,"devPct":t.devPct,"lpSize":t.lpSize,
        "holders":t.holders,
        "sma5":sma(pr,300) or 0,"sma15":sma(pr,900) or 0,
        "rsi14":rsi(pr,14) or 0,"atr14":atr(pr,14) or 0,"vol_5m":vol_5m(t)
    }

# ───────────────────── subscribe helper ────────────────────────
async def _subscribe(ws,mint):
    for m in ("subscribeTokenTrade","subscribeTokenTrades"):
        try: await ws.send(json.dumps({"method":m,"keys":[mint]}))
        except: pass

# ───────────────────── WebSocket handler ───────────────────────
async def on_pump(msg, ws):
    tx, mint = msg.get("txType"), msg.get("mint")
    if not mint: return

    # ── création ───────────────────────────────────────────────
    if tx=="create":
        t=TokenInfo(mint, abbreviate(mint))
        t.devPct  = msg.get("devPercent",0)
        t.lpSize  = msg.get("lpLamports",0)/1e9
        t.holders = msg.get("holderCount",0)
        tokens[mint]=t
        await _subscribe(ws,mint)
        return

    # ── trades ────────────────────────────────────────────────
    if tx in ("buy","sell") and mint in tokens:
        t=tokens[mint]

        # market-cap & volume
        t.mcap_sol = msg.get("marketCapSol", t.mcap_sol)
        dvol       = msg.get("marketCapSol", 0.0)

        # progress basé sur mcap / 69 k€
        mcap_eur = t.mcap_sol * sol_to_eur()
        t.progress = min(mcap_eur / MIGRATE_CAP_EUR, 1.0)

        # facultatifs
        if "holderCount" in msg: t.holders = msg["holderCount"]
        if "devPercent"  in msg: t.devPct  = msg["devPercent"]
        if "lpLamports"  in msg: t.lpSize  = msg["lpLamports"]/1e9

        # historique
        now=time.time()
        t.hist_mcap.append((now,t.mcap_sol))
        t.vol_hist.append((now,dvol))

        price_eur = mcap_eur

        # gestion positions
        if mint in open_trades:
            pos=open_trades[mint]
            if price_eur>pos["high"]:
                pos["high"]=price_eur
                pos["trail"]=max(pos["trail"],
                                 price_eur-PARAMS["sl_mult"]*(atr(_prices(t),14) or 0))
            if price_eur>=pos["tp"]:           _close(pos,price_eur,"TP")
            elif price_eur<=pos["trail"] or price_eur<=pos["stop"]:
                                               _close(pos,price_eur,"SL/Trail")
        else:
            if t.progress>=0.90 and _ia_ok(f:=_feats(t,now)):
                _open(t,f)
        return

    # ── migration ─────────────────────────────────────────────
    if tx=="migrate" and mint in open_trades:
        _close(open_trades[mint], tokens[mint].mcap_sol*sol_to_eur(), "GRAD")

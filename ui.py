# pumpfunbot/ui.py
import streamlit as st, pandas as pd, threading
from streamlit_autorefresh import st_autorefresh

from fetcher     import run_in_thread
from pf_helpers  import gmgn_link, sol_to_eur
from param_store import PARAMS
from strategy    import (
    tokens, open_trades, trades, balance,
    init_balance, on_pump,
    price_change_1m, vol_5m
)

# ───────────────────────── helper prix courant ────────────────
def cur_price_eur(tkn):
    """Retourne le ‘prix’ courant : MCap € (valeur stockée)"""
    return getattr(tkn, "price", tkn.mcap_sol * sol_to_eur())

# ───────────────────────── lancement WebSocket ────────────────
if "ws_started" not in st.session_state:
    init_balance(1_000.0)
    lock = threading.Lock()
    async def router(msg, ws): await on_pump(msg, ws)
    run_in_thread(router, lock)
    st.session_state["ws_started"] = True

# ───────────────────────── page / refresh ─────────────────────
st.set_page_config("Pump.fun Bot", "🤖", layout="wide")
st_autorefresh(interval=5_000, key="refresh")
st.title("🤖 Pump.fun Trading Bot — simulation")

# ───────────────────────── sidebar paramètres ─────────────────
def s(lbl, k, mn, mx, step, t=float):
    PARAMS[k] = t(
        st.sidebar.slider(lbl, t(mn), t(mx), t(PARAMS.get(k, mn)), step=t(step))
    )

s("Risk % / trade",  "risk_pct", 0.5, 10, 0.5, float)
s("TP × ATR",        "tp_mult",    1,  8, 0.5, float)
s("SL × ATR",        "sl_mult",    1,  4, 0.5, float)
s("Slippage %",      "slippage",   0, 10, 1,   int)

PARAMS["use_ai"] = st.sidebar.checkbox("🔮 Activer l’IA",
                                       value=PARAMS["use_ai"])
s("Seuil IA",      "ai_thresh", 0.5, 0.9, 0.05, float)
s("Max DD jour %", "max_dd_pct",   5, 50, 1,   float)

prog_min = st.sidebar.slider("Progression min %", 0, 100, 0)

st.sidebar.markdown("---")
st.sidebar.write(f"**SOL ≃ {sol_to_eur():.2f} €**")
st.sidebar.write(f"Tokens suivis : **{len(tokens)}**")
st.sidebar.write(
    f"Prêts à migrate : **{sum(1 for t in tokens.values() if t.progress>=0.95)}**"
)
if st.sidebar.button("🔄 Hard refresh"):
    st.rerun()

# ───────────────────────── KPIs globaux ───────────────────────
c1, c2, c3, c4, c5 = st.columns(5)

c1.metric("Solde €", f"{balance:,.2f}")

real_pnl = sum(t["pnl"] for t in trades)
c2.metric("PnL réalisé €", f"{real_pnl:,.2f}")

latent_pnl = sum(
    (cur_price_eur(tokens[m]) - tr["entry"]) * tr["qty"]
    for m, tr in open_trades.items()
)
c3.metric("PnL latent €", f"{latent_pnl:,.2f}")

if trades:
    wins = sum(1 for t in trades if t["pnl"] > 0)
    c4.metric("Win-rate %", f"{wins/len(trades)*100:,.1f}")

c5.metric("Pos. ouvertes", len(open_trades))
st.divider()

# ───────────────────────── tableau tokens ─────────────────────
rows = []
for t in tokens.values():
    if t.progress * 100 < prog_min:
        continue
    rows.append({
        "Token": f"[{t.name}]({gmgn_link(t.mint)})",
        "Prog %": f"{t.progress*100:.1f}",
        "MCap €": f"{cur_price_eur(t):,.0f}",
        "Δ 1 min %": f"{price_change_1m(t)*100:+.1f}",
        "Vol 5 m": f"{vol_5m(t):.2f}",
        "dev %": f"{t.devPct:.1f}",
        "LP SOL": f"{t.lpSize:.1f}",
        "Holders": t.holders
    })

st.subheader(f"Tokens ≥ {prog_min}% — n={len(rows)}")
st.table(rows if rows else [{"Info": "Aucun token"}])

# ───────────────────────── positions ouvertes ─────────────────
st.subheader("Positions ouvertes")
if open_trades:
    view = []
    for m, tr in open_trades.items():
        price = cur_price_eur(tokens[m])
        view.append({
            "Token": f"[{tokens[m].name}]({gmgn_link(m)})",
            "Entrée €": tr["entry"],
            "MCap €": price,
            "Qté": tr["qty"],
            "uPnL €": (price - tr["entry"]) * tr["qty"],
            "TP €": tr["tp"],
            "Stop €": tr["trail"]
        })
    df = pd.DataFrame(view)
    num_fmt = {c: "{:,.4f}" for c in
               ("Entrée €", "MCap €", "Qté", "uPnL €", "TP €", "Stop €")}
    st.dataframe(df.style.format(num_fmt), use_container_width=True)
else:
    st.caption("—")

# ───────────────────────── historique trades ──────────────────
st.subheader("Historique")
if trades:
    dfh = pd.DataFrame(trades).rename(columns={
        "name": "Token", "reason": "Type",
        "entry": "Entrée €", "exit": "Sortie €",
        "pnl": "PnL €", "pct": "PnL %"
    })
    # colonnes présentes uniquement
    num_fmt = {"Entrée €": "{:,.4f}", "Sortie €": "{:,.4f}",
               "PnL €": "{:,.2f}", "PnL %": "{:,.1f}"}
    cols = [c for c in ["Token", "Type"] + list(num_fmt) if c in dfh.columns]
    st.dataframe(
        dfh[cols].style.format({k: v for k, v in num_fmt.items() if k in dfh.columns}),
        use_container_width=True
    )
else:
    st.caption("—")

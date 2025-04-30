import requests, functools, time, html

# ── taux SOL → EUR ───────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _sol_eur():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids":"solana","vs_currencies":"eur"},
                         timeout=4)
        if r.ok:
            return r.json()["solana"]["eur"]
    except Exception:
        pass
    return 20.0
def sol_to_eur() -> float:           # TTL 600 s via cache
    return _sol_eur()

def compute_price_from_pool(v_sol, v_tok):
    return 0.0 if v_tok<=0 else (v_sol/v_tok)*sol_to_eur()

# ── divers ───────────────────────────────────────────────────────
def gmgn_link(mint:str) -> str:
    return f"https://gmgn.ai/sol/token/{mint}"
def abbreviate(addr:str) -> str:
    return addr[:4]+'…'+addr[-4:]

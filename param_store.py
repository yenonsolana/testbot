# Tous les sliders / cases à cocher de l’UI modifient ces valeurs à chaud
PARAMS = {
    # sizing
    "risk_pct":  1.0,     # % du capital risqué par trade
    # core strat
    "tp_mult":   4.0,     # multiplicateur ATR pour TP
    "sl_mult":   2.0,     # multiplicateur ATR pour SL
    # IA
    "use_ai":    False,
    "ai_thresh": 0.60,
    # risk-manager
    "max_dd_pct": 10.0,   # draw-down journalier %
    # notifications
    "webhook":   "",      # URL Discord / TG ; vide = off
}

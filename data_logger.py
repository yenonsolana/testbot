import csv, pathlib, time
OUT = pathlib.Path("training_set.csv")
FIELDS = ["ts","mint","name","progress","price",
          "devPct","lpSize","holders",
          "sma5","sma15","rsi14","atr14","vol_5m","label"]
def append(feats:dict, label:int):
    new = not OUT.exists()
    with OUT.open("a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=FIELDS)
        if new: wr.writeheader()
        row = {k: feats.get(k,0) for k in FIELDS}; row["label"]=label
        wr.writerow(row)

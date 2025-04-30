import time, pathlib, strategy, joblib
model=pathlib.Path("model.joblib"); last=0
while True:
    if model.exists() and model.stat().st_mtime>last:
        strategy.clf=joblib.load(model)
        last=model.stat().st_mtime
        print("🔄 Nouveau modèle rechargé")
    time.sleep(60*15)        # check toutes les 15 min

import pandas as pd, joblib, sklearn.ensemble, sklearn.model_selection, pathlib, datetime
CSV=pathlib.Path("training_set.csv")
if not CSV.exists(): raise SystemExit("Pas de training_set.csv")
df=pd.read_csv(CSV)
X=df[["progress","price","devPct","lpSize","holders","sma5","sma15","rsi14","atr14","vol_5m"]]
y=df["label"]
Xtr,Xte,ytr,yte=sklearn.model_selection.train_test_split(X,y,test_size=0.2,random_state=42, stratify=y)
clf=sklearn.ensemble.GradientBoostingClassifier(n_estimators=300,max_depth=3)
clf.fit(Xtr,ytr)
print("Accuracy",clf.score(Xte,yte))
ts=datetime.datetime.utcnow().strftime("%Y%m%d_%H%M")
joblib.dump(clf,"model.joblib")
print("model.joblib sauvegardé",ts)

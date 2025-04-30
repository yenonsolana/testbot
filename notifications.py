import requests, datetime, param_store as ps

def send(evt:str, text:str):
    url = ps.PARAMS["webhook"]
    if not url: return
    payload = {"content": f"**{evt}** – {text} – {datetime.datetime.utcnow():%H:%M:%S} UTC"}
    try:
        requests.post(url, json=payload, timeout=4)
    except Exception:     # on n’interrompt jamais le bot pour ça
        pass

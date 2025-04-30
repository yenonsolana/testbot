# pumpfunbot/fetcher.py
import asyncio, json, threading, time, websockets

URI   = "wss://pumpportal.fun/api/data"
PAUSE = 3          # secondes avant une reconnexion
PING  = 60         # délai max sans message avant reconnexion forcée

def _listen(callback, lock):
    async def worker():
        while True:
            try:
                async with websockets.connect(URI, ping_interval=None) as ws:
                    # souscriptions
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    last = time.time()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=PING)
                            last = time.time()
                        except asyncio.TimeoutError:
                            print("[WS] Timeout, reconnect…")
                            break                     # sort du while → reconnect
                        with lock:
                            await callback(json.loads(raw), ws)
            except Exception as e:
                print("[WS] erreur :", e)
            await asyncio.sleep(PAUSE)                # back-off

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(worker())

def run_in_thread(callback, lock):
    # évite de lancer plusieurs threads si Streamlit se recharge
    if getattr(run_in_thread, "_started", False):
        return
    t = threading.Thread(target=_listen, args=(callback, lock), daemon=True)
    t.start()
    run_in_thread._started = True

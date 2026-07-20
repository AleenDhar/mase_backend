import time
LOG="cc_work/_retry2.log"
t0=time.time()
while time.time()-t0<2000:
    time.sleep(45)
    try: txt=open(LOG,encoding="utf-8",errors="replace").read()
    except Exception: continue
    if "RETRY2-DONE" in txt:
        for l in txt.splitlines():
            if "retry ->" in l or "TIMEOUT" in l: print(l.strip()[:100], flush=True)
        print("RETRY2WATCH-DONE", flush=True); break
else:
    print("RETRY2WATCH-TIMEOUT", flush=True)

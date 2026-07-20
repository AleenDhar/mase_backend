import time
LOG="cc_work/_retry_qual2.log"
t0=time.time()
while time.time()-t0<3200:
    time.sleep(60)
    try: txt=open(LOG,encoding="utf-8",errors="replace").read()
    except Exception: continue
    if "RETRY-QUAL2-DONE" in txt:
        for l in txt.splitlines():
            if "from-scratch ->" in l or "TIMEOUT" in l: print(l.strip()[:100], flush=True)
        print("RQ2-DONE", flush=True); break
else:
    print("RQ2-EXIT", flush=True)

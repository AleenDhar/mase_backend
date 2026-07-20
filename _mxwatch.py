import time
LOG="cc_work/_maxis_scratch.log"
t0=time.time()
while time.time()-t0<1500:
    time.sleep=__import__("time").sleep; time.sleep(40)
    try: txt=open(LOG,encoding="utf-8",errors="replace").read()
    except Exception: continue
    if "MAXIS-SCRATCH-DONE" in txt or "TIMEOUT" in txt:
        for l in txt.splitlines():
            if l.strip(): print(l.strip()[:120], flush=True)
        print("MXWATCH-DONE", flush=True); break
else:
    print("MXWATCH-EXIT", flush=True)

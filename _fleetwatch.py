"""Watch the 253-deal fleet log; print progress periodically; exit on FLEET-LIVE-DONE or timeout."""
import time, re, os
LOG="cc_work/_fleet_live.log"
t0=time.time()
last=""
while time.time()-t0<10800:  # 3h cap
    time.sleep(90)
    try:
        txt=open(LOG,encoding="utf-8",errors="replace").read()
    except Exception:
        continue
    lines=[l for l in txt.splitlines() if l.strip()]
    okc=sum(1 for l in lines if " OK  " in l)
    skc=sum(1 for l in lines if " SKIP " in l)
    toc=sum(1 for l in lines if " TO  " in l)
    tail=lines[-1] if lines else ""
    print(f"progress: OK={okc} SKIP={skc} TO={toc} done={okc+skc+toc}/253 | {tail[:90]}", flush=True)
    if "FLEET-LIVE-DONE" in txt:
        # print the summary tail
        for l in lines[-20:]:
            if any(k in l for k in ("on v10.10","retired","Reconciler","FLEET-LIVE-DONE","scaling")):
                print("  "+l, flush=True)
        print("WATCH-DONE", flush=True); break
else:
    print("WATCH-TIMEOUT", flush=True)

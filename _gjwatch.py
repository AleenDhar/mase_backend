import time
LOG="cc_work/_fleet_gj.log"
t0=time.time()
while time.time()-t0<3000:
    time.sleep(75)
    try: txt=open(LOG,encoding="utf-8",errors="replace").read()
    except Exception: continue
    L=[l for l in txt.splitlines() if l.strip()]
    ok=sum(1 for l in L if l.strip().startswith("[") and " OK " in l); to=sum(1 for l in L if " TO " in l)
    print(f"progress: {ok+to}/15 done | {(L[-1][:80] if L else '')}", flush=True)
    if "FLEET-GJ-DONE" in txt:
        for l in L[-4:]:
            if l.strip(): print("  "+l[:90], flush=True)
        print("GJWATCH-DONE", flush=True); break
else:
    print("GJWATCH-TIMEOUT", flush=True)

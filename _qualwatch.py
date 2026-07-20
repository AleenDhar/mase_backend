import time
LOG="cc_work/_fleet_qualified.log"
t0=time.time(); last=0
while time.time()-t0<9000:  # 2.5h
    time.sleep(120)
    try: txt=open(LOG,encoding="utf-8",errors="replace").read()
    except Exception: continue
    L=[l for l in txt.splitlines() if l.strip()]
    ok=sum(1 for l in L if " OK " in l); to=sum(1 for l in L if " TO " in l)
    if ok+to!=last: print(f"progress: {ok} ok / {to} timeout | {(L[-1][:80] if L else '')}", flush=True); last=ok+to
    if "FLEET-QUALIFIED-DONE" in txt:
        for l in L[-3:]:
            if l.strip(): print("  "+l[:90], flush=True)
        print("QUALWATCH-DONE", flush=True); break
else:
    print("QUALWATCH-EXIT", flush=True)

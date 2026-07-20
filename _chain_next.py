"""Autonomous chain: wait for the 253 live fleet (FLEET-LIVE-DONE), then fire the 62-deal
forecasted+Woodcock fleet (conc28/ECS30, resume-skips v10.10), then wait for FLEET-NEXT-DONE.
Prints progress; notifies at CHAIN-DONE. Never double-fires (this is the ONLY launcher of next)."""
import sys, time, subprocess, os
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
LIVE_LOG="cc_work/_fleet_live.log"; NEXT_LOG="cc_work/_fleet_next.log"
def read(p):
    try: return open(p,encoding="utf-8",errors="replace").read()
    except Exception: return ""
def counts(txt,n):
    L=[l for l in txt.splitlines() if l.strip()]
    ok=sum(1 for l in L if " OK  " in l); sk=sum(1 for l in L if " SKIP " in l); to=sum(1 for l in L if " TO  " in l)
    return ok,sk,to,(L[-1][:88] if L else "")

# --- Phase A: wait for the 253 fleet to finish ---
print("[chain] waiting for 253 live fleet to finish…", flush=True)
t0=time.time()
_done=False
while time.time()-t0<10800:   # 3h — must exceed the 253 fleet's real runtime (~2.5h at ~12min/deal)
    time.sleep(90)
    txt=read(LIVE_LOG)
    ok,sk,to,tail=counts(txt,253)
    print(f"[chain] 253-fleet: OK={ok} SKIP={sk} TO={to} ({ok+sk+to}/253) | {tail}", flush=True)
    if "FLEET-LIVE-DONE" in txt:
        print("[chain] 253 fleet DONE.", flush=True)
        for l in txt.splitlines()[-6:]:
            if l.strip(): print("   "+l[:100], flush=True)
        _done=True
        break
if not _done:
    # SAFETY: never fire the 62 while the 253 might still be running — its ECS scale-back
    # to 2 would starve the 62 mid-run. Abort and let it be fired manually.
    print("[chain] TIMEOUT (3h) — ABORTING; will NOT fire 62 (avoid scale collision).", flush=True)
    print("CHAIN-DONE", flush=True)
    raise SystemExit(0)

# --- Phase B: fire the 62-deal forecasted+Woodcock fleet ---
time.sleep(25)  # let the 253 fleet's ECS scale-back settle so blue is free for the 62's scale-up
print("[chain] launching _fleet_next.py (62 forecasted+Woodcock, conc28/ECS30)…", flush=True)
with open(NEXT_LOG,"w",encoding="utf-8") as fh:
    p=subprocess.Popen([sys.executable,"_fleet_next.py"],stdout=fh,stderr=subprocess.STDOUT)

# --- Phase C: wait for the 62 fleet to finish ---
t1=time.time()
while time.time()-t1<3600:
    time.sleep(90)
    txt=read(NEXT_LOG)
    ok,sk,to,tail=counts(txt,62)
    print(f"[chain] 62-fleet: OK={ok} SKIP={sk} TO={to} ({ok+sk+to}/62) | {tail}", flush=True)
    if "FLEET-NEXT-DONE" in txt:
        print("[chain] 62 fleet DONE.", flush=True)
        for l in txt.splitlines()[-18:]:
            if any(k in l for k in ("on v10.10","retired","Reconciler","FLEET-NEXT-DONE","scaling back")):
                print("   "+l[:100], flush=True)
        break
    if p.poll() is not None and "FLEET-NEXT-DONE" not in txt:
        print(f"[chain] _fleet_next.py exited (code {p.returncode}).", flush=True); break
else:
    print("[chain] TIMEOUT waiting for 62 fleet.", flush=True)
print("CHAIN-DONE", flush=True)

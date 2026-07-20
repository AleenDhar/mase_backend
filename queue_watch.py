"""Watch the deploy land, then watch the durable queue drain.

Deploy detection is BEHAVIOURAL (no AWS CLI on this box): the new code routes a manual
trigger through enqueue_trigger, which writes a `sweep_queue` row. The old code never did.
So: a fresh sweep_queue row for one of our opps == the new task def is serving traffic.

Read-only until `arm` is passed.
  python queue_watch.py            # poll queue + deal_records, no writes
  python queue_watch.py arm        # additionally re-trigger any deal not yet done
"""
import sys, time, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for _l in open(ENV, encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
SH = {"apikey": cfg["SUPABASE_SERVICE_ROLE_KEY"],
      "Authorization": f"Bearer {cfg['SUPABASE_SERVICE_ROLE_KEY']}"}

DEALS = [("SAMI", "006P700000RD9Ir"), ("Allstate", "006P7000006uKrq"),
         ("Robert Bosch", "006P700000PlMpu"), ("NORTHPORT", "006P700000QFJwD"),
         ("Domino's Pizza", "006P700000X6hvK"), ("Greencore", "006P700000WeRX8"),
         ("SARS", "006P700000UZv8c"), ("Etex Group", "006P700000UGPE5")]
NTOTAL = len(DEALS)
OIDS = [o for _, o in DEALS]
SEL = "updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio"


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def queue_rows():
    """sweep_queue is keyed on opp_id (no `id` column); one upserted row per opp."""
    try:
        r = requests.get(f"{SB}/rest/v1/sweep_queue",
                         params={"select": "opp_id,status,attempts,account_name,run_id,error,"
                                           "duration_ms,claimed_at,created_at,updated_at",
                                 "order": "updated_at.desc", "limit": "60"},
                         headers=SH, verify=False, timeout=(10, 45))
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code} {r.text[:110]}"
        return r.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def done_map():
    out = {}
    for lbl, oid in DEALS:
        try:
            r = requests.get(f"{SB}/rest/v1/deal_records",
                             params={"select": SEL, "opp_id": f"eq.{oid}"},
                             headers=SH, verify=False, timeout=(10, 45)).json()
            if not r:
                out[lbl] = ("NOROW", None, None); continue
            r = r[0]
            ds = r.get("scores") or {}
            hl = ds.get("headline") or {}
            sv = (r.get("studio") or {}).get("versions") or {}
            fresh = str(r.get("updated_at") or "") > "2026-07-09T14:00:45"
            good = fresh and ds.get("factor_source") == "ai" and not ds.get("scoring_degraded") \
                and str(sv.get("win")) == "10.7"
            out[lbl] = ("DONE" if good else ("STALE" if not fresh else "DEGRADED"),
                        hl.get("win_position"), hl.get("deal_momentum"))
        except Exception as e:
            out[lbl] = (f"ERR:{type(e).__name__}", None, None)
    return out


# The fix was pushed to main at ~14:46 UTC. A sweep_queue row touched AFTER that can only
# have been written by the new enqueue_trigger path -> proves the new task def serves traffic.
PUSH_TS = "2026-07-09T14:46:00"

# ZERO-COST deploy fingerprint. Both paths reject an opp that isn't in the book, but at
# different moments:
#   OLD  trigger_opp_async -> returns "accepted" SYNCHRONOUSLY (membership is checked later,
#        inside the fire-and-forget task, which then returns not_in_book to nobody).
#   NEW  await enqueue_trigger -> awaits is_active_member first, so it returns "not_in_book".
# Neither spends an LLM token. A bogus id therefore tells us which code is serving.
PROBE_OID = "006000000000000AAA"


def new_code_serving():
    try:
        r = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                          json={"opp_id": PROBE_OID, "source": "manual"},
                          verify=False, timeout=(10, 40))
        res = ((r.json() or {}).get("results") or {}).get(PROBE_OID)
        return res == "not_in_book", res
    except Exception as e:
        return False, f"{type(e).__name__}"


def mcp_ready():
    """A trigger that lands on a task whose MCP tools haven't loaded dies instantly with
    `RuntimeError: no salesforce/avoma tools loaded yet` (observed dur=1ms during the rolling
    deploy). Never fire until the task actually reports salesforce + avoma ready."""
    try:
        h = requests.get(f"{API}/api/health", verify=False, timeout=(8, 25)).json()
        srv = h.get("mcp_servers") or {}
        ok = (h.get("mcp_tools_loaded") is True
              and str(srv.get("salesforce", "")).startswith("ready")
              and str(srv.get("avoma", "")).startswith("ready"))
        return ok, f"tools={h.get('mcp_tools_loaded')} sf={srv.get('salesforce')} avoma={srv.get('avoma')}"
    except Exception as e:
        return False, f"{type(e).__name__}"


def main():
    arm = len(sys.argv) > 1 and sys.argv[1] == "arm"
    print(f"[{ts()}] queue_watch (arm={arm}) — waiting for deploy, then draining\n", flush=True)
    deploy_seen = False
    waiting_since = None      # STALL ALARM: rows enqueued but no worker ever claims them.
    # worker.py claims via the secret-gated claim_one_sweep_v2(p_secret) RPC. Without
    # SWEEP_QUEUE_SECRET it gets an empty result FOREVER — indistinguishable from "queue
    # drained", never an error. Combined with a worker fleet that failed to autoscale off 0,
    # that is a silent stall. If rows sit `waiting` with none ever going `working`, say so.
    last_fire = {}
    while True:
        if not deploy_seen:
            newc, pres = new_code_serving()
            if newc:
                deploy_seen = True
                print(f"[{ts()}] *** DEPLOY LANDED — probe returned {pres!r} (awaited "
                      f"enqueue_trigger). Manual triggers are now durable + worker-drained.",
                      flush=True)
            else:
                print(f"[{ts()}] deploy not live yet (probe={pres!r}, old code returns "
                      f"'accepted' synchronously)", flush=True)
        qr, qerr = queue_rows()
        dm = done_map()
        ndone = sum(1 for v in dm.values() if v[0] == "DONE")
        mine = [x for x in (qr or []) if x.get("opp_id") in OIDS]
        if qerr:
            print(f"[{ts()}] sweep_queue read: {qerr}", flush=True)
        else:
            states = {}
            for x in mine:
                states[x.get("status", "?")] = states.get(x.get("status", "?"), 0) + 1
            if any(str(x.get("updated_at") or "") > PUSH_TS for x in mine) and not deploy_seen:
                deploy_seen = True
                print(f"[{ts()}] *** DEPLOY LANDED — manual triggers are now enqueued "
                      f"(durable, worker-drained)", flush=True)
            print(f"[{ts()}] done={ndone}/{NTOTAL}  my queue rows={len(mine)} {states or ''}"
                  f"{'' if deploy_seen else '  (old code still serving)'}", flush=True)
            nwait, nwork = states.get("waiting", 0), states.get("working", 0)
            if nwait and not nwork:
                waiting_since = waiting_since or time.time()
                stalled = time.time() - waiting_since
                if stalled > 360:
                    print(f"[{ts()}] !!! QUEUE STALL — {nwait} row(s) `waiting`, 0 `working` "
                          f"for {int(stalled)//60}m. The worker is not claiming. Check: "
                          f"(a) mase-worker desiredCount still 0 (autoscaler), "
                          f"(b) SWEEP_QUEUE_SECRET missing in worker env -> claim_one_sweep_v2 "
                          f"returns empty forever. FALLBACK: revert the server.py routing "
                          f"commit to go back in-process (now 4GB api + trigger_conc=8).",
                          flush=True)
            else:
                waiting_since = None
            if nwork:
                print(f"[{ts()}] *** WORKER IS CLAIMING — {nwork} row(s) `working` "
                      f"(durable path proven end-to-end)", flush=True)
        for lbl, (st, w, m) in dm.items():
            if st != "DONE":
                print(f"           {lbl:15} {st} win={w} mom={m}", flush=True)
        if ndone == NTOTAL:
            print(f"\n[{ts()}] ALL 7 DONE", flush=True)
            return
        # Only trigger once the NEW code serves traffic. Re-triggering into the old
        # fire-and-forget path during a rolling deploy just burns Anthropic tokens on sweeps
        # the next task replacement will kill. A run already in flight is unaffected.
        if arm and deploy_seen:
            ready, why = mcp_ready()
            if not ready:
                print(f"[{ts()}] holding fire — MCP not ready ({why})", flush=True)
            else:
                for lbl, oid in DEALS:
                    if dm[lbl][0] == "DONE":
                        continue
                    if any(x.get("opp_id") == oid and x.get("status") in ("waiting", "working")
                           for x in mine):
                        continue
                    if time.time() - last_fire.get(oid, 0) < 300:
                        continue          # never hammer the same opp inside 5 minutes
                    r = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                                      json={"opp_id": oid, "source": "manual"}, verify=False,
                                      timeout=60)
                    try:
                        res = ((r.json() or {}).get("results") or {}).get(oid)
                    except Exception:
                        res = r.text[:60]
                    last_fire[oid] = time.time()
                    print(f"[{ts()}] >>> trigger {lbl}: HTTP {r.status_code} res={res}",
                          flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()

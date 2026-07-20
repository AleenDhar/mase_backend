"""LIVE sweeps on AWS — fan out N opportunities in parallel against the deployed API.

No AWS CLI, no dryrun_fleet import (both stall behind Zscaler on this laptop). Creds are
read straight from the frontend .env.local. The WORK happens on AWS ECS: each POST to
/api/deal-engine/sweep/trigger returns 202 and the deployed pipeline runs the sweep there.
This script only fires the triggers and polls Supabase for the resulting deal_records row.

WRITES deal_records (that is the point — the user asked for a live cloud sweep).
Never touches Salesforce.

Usage:
  python aws_sweep.py find <name>     # resolve an opp_id by account/opp name (read-only)
  python aws_sweep.py baseline        # read-only: current scores for the target set
  python aws_sweep.py run             # trigger all targets in parallel + watch + QA
"""
import csv, json, os, re, subprocess, sys, time, warnings
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
SKEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": SKEY, "Authorization": f"Bearer {SKEY}"}
VERIFY = False          # Zscaler re-signs TLS; traffic is already proxy-intercepted

# Project only the nested paths we need — `select=record` pulls a multi-MB blob per deal.
SELECT = ("opp_id,account_name,updated_at,swept_at,"
          "scores:record->ai->deal_scores,studio:record->ai->scoring_studio")

TARGETS = [
    ("SAMI",           "006P700000RD9Ir"),
    ("Allstate",       "006P7000006uKrq"),
    ("Robert Bosch",   "006P700000PlMpu"),
    ("NORTHPORT",      "006P700000QFJwD"),
    ("Domino's Pizza", "006P700000X6hvK"),
    ("Greencore",      "006P700000WeRX8"),
    ("SARS",           None),             # resolved at runtime by name
]
POLL_S = 45
TIMEOUT_S = 2700


def sb(params, table="deal_records"):
    r = requests.get(f"{SB}/rest/v1/{table}", params=params, headers=SH,
                     verify=VERIFY, timeout=(10, 60))
    r.raise_for_status()
    return r.json()


def find(name):
    hits = []
    for col in ("account_name", "opp_name"):
        try:
            hits += sb({"select": "opp_id,account_name,opp_name,stage,forecast_category",
                        col: f"ilike.*{name}*"})
        except Exception as e:
            print(f"  [{col}] err {e}")
    seen, out = set(), []
    for h in hits:
        if h["opp_id"] not in seen:
            seen.add(h["opp_id"]); out.append(h)
    return out


def state(oid):
    r = sb({"select": SELECT, "opp_id": f"eq.{oid}"})
    if not r:
        return None
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {"account": r.get("account_name"), "updated_at": r.get("updated_at"),
            "swept_at": r.get("swept_at"), "win": hl.get("win_position"),
            "mom": hl.get("deal_momentum"), "commit": hl.get("customer_commitment"),
            "risk": hl.get("deal_risk"), "src": ds.get("factor_source"),
            "win_engine": sv.get("win"), "mom_engine": sv.get("mom")}


def trigger(oid):
    r = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                      json={"opp_id": oid, "source": "manual"}, verify=VERIFY, timeout=60)
    return r.status_code, r.text[:160]


def local_csv():
    out = {}
    try:
        for r in csv.DictReader(open("cc_fleet_results.csv", encoding="utf-8-sig")):
            out[r["opp_id"]] = (r["win"], r["momentum"])
    except Exception:
        pass
    return out


def resolve_targets():
    out = []
    for label, oid in TARGETS:
        if oid:
            out.append((label, oid)); continue
        hits = find("SARS") or find("SOUTH AFRICAN")
        if not hits:
            print(f"[warn] could not resolve {label} — skipping"); continue
        h = hits[0]
        print(f"[resolved] {label} -> {h['opp_id']}  {h['account_name']} | {h.get('stage')}")
        out.append((label, h["opp_id"]))
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "baseline"

    if cmd == "find":
        for h in find(sys.argv[2]):
            print(h)
        sys.exit(0)

    try:
        h = requests.get(f"{API}/api/health", verify=VERIFY, timeout=(10, 30))
        print(f"[api] {API} -> {h.status_code} {h.text[:100]}\n", flush=True)
    except Exception as e:
        print(f"[api] UNREACHABLE {type(e).__name__}: {e}"); sys.exit(1)

    TG = resolve_targets()
    LOCAL = local_csv()

    if cmd == "baseline":
        print("\nBASELINE (read-only)")
        for label, oid in TG:
            s = state(oid)
            if not s:
                print(f"  {label:15} NO ROW"); continue
            lw = LOCAL.get(oid, ("-", "-"))
            print(f"  {label:15} upd={str(s['updated_at'])[:19]} win={s['win']} mom={s['mom']} "
                  f"src={s['src']} winEng=v{s['win_engine']} | localCSV {lw[0]}/{lw[1]}")
        sys.exit(0)

    print(f"=== LIVE AWS SWEEP: {len(TG)} deals fired in parallel ===\n", flush=True)
    inflight = {}
    for label, oid in TG:
        base = state(oid)
        code, body = trigger(oid)
        print(f"[trigger] {label:15} {oid} -> HTTP {code}{'  ' + body if code >= 300 else ''}",
              flush=True)
        inflight[oid] = {"label": label, "base": (base or {}).get("updated_at"), "t0": time.time()}
        time.sleep(1.5)

    print(f"\n[watch] polling deal_records.updated_at every {POLL_S}s "
          f"(cap {TIMEOUT_S // 60}m per deal)\n", flush=True)
    results = []
    while inflight:
        time.sleep(POLL_S)
        for oid in list(inflight):
            st = inflight[oid]
            age = int(time.time() - st["t0"])
            try:
                cur = state(oid)
            except Exception as e:
                print(f"  [poll err] {st['label']}: {type(e).__name__}", flush=True); continue
            if cur and cur.get("updated_at") != st["base"]:
                print(f"\n[done {age // 60}m{age % 60:02d}s] {st['label']} — win={cur['win']} "
                      f"mom={cur['mom']} commit={cur['commit']} risk={cur['risk']} "
                      f"src={cur['src']} winEng=v{cur['win_engine']}", flush=True)
                acc = p_ = f_ = w_ = "?"
                try:
                    p = subprocess.run([sys.executable, "qa_live.py", oid, st["label"]],
                                       capture_output=True, text=True, timeout=300)
                    out = (p.stdout or "") + (p.stderr or "")
                    print(out, flush=True)
                    m = re.search(r"PASS (\d+) / FAIL (\d+) / WARN (\d+)\s+->\s+accuracy (\d+)%", out)
                    if m:
                        p_, f_, w_, acc = m.group(1), m.group(2), m.group(3), m.group(4) + "%"
                except Exception as e:
                    print(f"  [qa err] {type(e).__name__}: {e}", flush=True)
                results.append({"label": st["label"], "oid": oid, "sec": age, "cur": cur,
                                "pass": p_, "fail": f_, "warn": w_, "acc": acc})
                del inflight[oid]
            elif age > TIMEOUT_S:
                print(f"[TIMEOUT] {st['label']} after {age // 60}m", flush=True)
                results.append({"label": st["label"], "oid": oid, "sec": age, "cur": cur or {},
                                "acc": "TIMEOUT", "pass": "-", "fail": "-", "warn": "-"})
                del inflight[oid]
            else:
                print(f"  … {st['label']:15} {age // 60}m{age % 60:02d}s", flush=True)

    print("\n" + "=" * 104, flush=True)
    print("LIVE AWS SCORECARD", flush=True)
    print("=" * 104, flush=True)
    print(f"{'deal':16} {'cloud win/mom':>14} {'localCSV':>10} {'src':>7} {'winEng':>7} "
          f"{'mins':>5} {'accuracy':>9}  P/F/W", flush=True)
    for r in sorted(results, key=lambda x: x["label"]):
        c = r["cur"]; lw = LOCAL.get(r["oid"], ("-", "-"))
        print(f"{r['label']:16} {str(c.get('win')) + '/' + str(c.get('mom')):>14} "
              f"{lw[0] + '/' + lw[1]:>10} {str(c.get('src')):>7} {'v' + str(c.get('win_engine')):>7} "
              f"{r['sec'] // 60:>5} {r['acc']:>9}  {r['pass']}/{r['fail']}/{r['warn']}", flush=True)
    json.dump(results, open("cc_work/_aws_sweep.json", "w"), indent=2, default=str)
    print("\nwrote cc_work/_aws_sweep.json", flush=True)

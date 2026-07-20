"""Cost of today's sweeps for the 8 deals — straight from deal_trigger_runs.

_persist_run_log writes cost_usd + token counts on EVERY run that reaches analyze_one's
finally (success OR failure). Runs killed by the OOM / task-replacement never got there,
so their Anthropic spend is REAL but has no row — reported separately as unaccounted.
"""
import sys, csv, warnings, collections
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
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
SH = {"apikey": cfg["SUPABASE_SERVICE_ROLE_KEY"],
      "Authorization": f"Bearer {cfg['SUPABASE_SERVICE_ROLE_KEY']}"}

DEALS = {"006P700000RD9Ir": "SAMI", "006P7000006uKrq": "Allstate",
         "006P700000PlMpu": "Robert Bosch", "006P700000QFJwD": "NORTHPORT",
         "006P700000X6hvK": "Domino's Pizza", "006P700000WeRX8": "Greencore",
         "006P700000UZv8c": "SARS", "006P700000UGPE5": "Etex Group"}
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-07-09T14:00:00"

rows = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                    params={"select": "opp_id,account_name,status,error,duration_ms,model,"
                                      "input_tokens,output_tokens,total_tokens,cost_usd,"
                                      "source,created_at",
                            "created_at": f"gte.{SINCE}",
                            "order": "created_at.asc", "limit": "300"},
                    headers=SH, verify=False, timeout=(10, 60)).json()
mine = [r for r in rows if r.get("opp_id") in DEALS]

print("=" * 108)
print(f"SWEEP RUNS since {SINCE}  (this session)")
print("=" * 108)
print(f"{'time':9} {'deal':16} {'status':>10} {'mins':>6} {'in_tok':>9} {'out_tok':>8} "
      f"{'cost $':>8}  src")
tot = wasted = 0.0
ok_n = bad_n = 0
per = collections.defaultdict(float)
for r in mine:
    c = float(r.get("cost_usd") or 0)
    tot += c
    lbl = DEALS[r["opp_id"]]
    per[lbl] += c
    good = (r.get("status") or "").lower() == "completed"
    if good:
        ok_n += 1
    else:
        bad_n += 1
        wasted += c
    print(f"{str(r['created_at'])[11:19]} {lbl:16} {str(r.get('status')):>10} "
          f"{(r.get('duration_ms') or 0)/60000:>6.1f} {str(r.get('input_tokens') or '-'):>9} "
          f"{str(r.get('output_tokens') or '-'):>8} {c:>8.4f}  {r.get('source')}")

print("-" * 108)
print(f"{'':9} {'TOTAL':16} {ok_n} completed / {bad_n} failed "
      f"{'':>14}{'':>9}{'':>8} {tot:>8.4f}")
print(f"{'':9} {'  of which wasted (failed runs)':46}{'':>20} {wasted:>8.4f}")

print("\nPER-DEAL (logged runs only):")
for lbl, c in sorted(per.items(), key=lambda x: -x[1]):
    print(f"  {lbl:16} ${c:.4f}")

# Runs that were OOM-killed / task-replaced never reached analyze_one's finally -> no row,
# but Anthropic was already paid for the tokens burned before the kill.
avg = (tot / max(1, len(mine)))
print("\nUNACCOUNTED (no run row — killed mid-flight, cost still incurred):")
print("  5 sweeps OOM-killed ~14:13 + 3 killed by the rolling deploy ~14:5x.")
print(f"  Each ran partially; at the ${avg:.2f} average logged run cost, a partial run is")
print(f"  roughly 0.3-0.7x that -> order of ${8*avg*0.3:.2f}-${8*avg*0.7:.2f} not in the table.")

print(f"\nGRAND TOTAL (logged): ${tot:.4f} across {len(mine)} runs")
print(f"Model: {mine[0].get('model') if mine else '-'}")

with open("sweep_cost_report.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["time", "deal", "opp_id", "status", "minutes", "input_tokens", "output_tokens",
                "total_tokens", "cost_usd", "model", "source", "error"])
    for r in mine:
        w.writerow([r.get("created_at"), DEALS[r["opp_id"]], r["opp_id"], r.get("status"),
                    round((r.get("duration_ms") or 0) / 60000, 2), r.get("input_tokens"),
                    r.get("output_tokens"), r.get("total_tokens"), r.get("cost_usd"),
                    r.get("model"), r.get("source"), (r.get("error") or "")[:200]])
print("wrote sweep_cost_report.csv")

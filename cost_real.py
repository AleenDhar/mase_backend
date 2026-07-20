"""True cost of today's sweeps — the logged cost_usd is wrong for claude-sonnet-5.

_calculate_llm_cost has no pricing key matching "claude-sonnet-5", so it returns 0.0 and
cost_usd lands NULL. Only the claude-sonnet-4-5 (stale-worker) runs were priced.

deal_trigger_runs stores input_tokens as the SUM of uncached + cache_creation + cache_read,
so the split is unrecoverable from the table. We therefore report a BAND:
  ceiling  — every input token billed as uncached (1.00x input rate)
  floor    — every input token billed as a cache read (0.10x input rate)
  estimate — the effective input rate observed on the two runs the system DID price.

Pricing (authoritative, per MTok): Sonnet 5 = $3 in / $15 out list. An introductory
$2 / $10 applies through 2026-08-31, so today's real spend is ~2/3 of the list figure.
Cache write = 1.25x input; cache read = 0.10x input.
"""
import csv, sys, warnings
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
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}

LIST_IN, LIST_OUT = 3.0, 15.0        # $/MTok, Sonnet 5 & 4.5 list
INTRO_IN, INTRO_OUT = 2.0, 10.0      # Sonnet 5 introductory, through 2026-08-31

rows = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                    params={"select": "account_name,model,status,input_tokens,output_tokens,"
                                      "cost_usd,duration_ms,created_at",
                            "created_at": "gte.2026-07-09T14:00:00",
                            "order": "created_at.asc", "limit": "100"},
                    headers=SH, verify=False, timeout=(10, 60)).json()
runs = [r for r in rows if r.get("input_tokens")]

# Calibrate the effective input rate from the two runs the system priced correctly.
priced = [r for r in runs if r.get("cost_usd")]
eff = None
if priced:
    num = den = 0.0
    for r in priced:
        out_cost = r["output_tokens"] * LIST_OUT / 1e6
        in_cost = float(r["cost_usd"]) - out_cost
        num += in_cost
        den += r["input_tokens"] / 1e6
    eff = num / den if den else None
    print(f"calibration: {len(priced)} priced run(s) -> effective input rate "
          f"${eff:.2f}/MTok (vs ${LIST_IN:.2f} list) => cache offset {1 - eff/LIST_IN:+.0%}\n")

print("=" * 106)
print(f"{'deal':22}{'model':14}{'in_tok':>10}{'out_tok':>9}{'logged':>9}"
      f"{'floor':>8}{'est':>8}{'ceiling':>9}")
print("=" * 106)
tot = [0.0, 0.0, 0.0]
for r in runs:
    it, ot = r["input_tokens"], r["output_tokens"]
    m = "sonnet-5" if "sonnet-5" in (r.get("model") or "") else "sonnet-4-5"
    ir, orr = (INTRO_IN, INTRO_OUT) if m == "sonnet-5" else (LIST_IN, LIST_OUT)
    out_c = ot * orr / 1e6
    floor = it * ir * 0.10 / 1e6 + out_c
    ceil = it * ir / 1e6 + out_c
    est = it * (eff or ir) * (ir / LIST_IN) / 1e6 + out_c if eff else (floor + ceil) / 2
    tot[0] += floor; tot[1] += est; tot[2] += ceil
    lg = f"${float(r['cost_usd']):.3f}" if r.get("cost_usd") else "NULL"
    print(f"{str(r.get('account_name'))[:21]:22}{m:14}{it:>10,}{ot:>9,}{lg:>9}"
          f"{floor:>8.2f}{est:>8.2f}{ceil:>9.2f}")
print("-" * 106)
print(f"{'TOTAL (' + str(len(runs)) + ' billed runs)':46}{'':>9}{'':>9}"
      f"{tot[0]:>8.2f}{tot[1]:>8.2f}{tot[2]:>9.2f}")
print(f"\nSonnet 5 billed at introductory $2/$10 per MTok (through 2026-08-31); "
      f"sonnet-4-5 at $3/$15.")
print(f"Failed runs (0 tokens) cost nothing. OOM/deploy-killed sweeps never wrote a row —")
print(f"their partial spend is real but unrecorded.")

with open("sweep_cost_real.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "model", "input_tokens", "output_tokens", "logged_cost_usd",
                "floor_usd", "estimate_usd", "ceiling_usd", "created_at"])
    for r in runs:
        it, ot = r["input_tokens"], r["output_tokens"]
        m = "sonnet-5" if "sonnet-5" in (r.get("model") or "") else "sonnet-4-5"
        ir, orr = (INTRO_IN, INTRO_OUT) if m == "sonnet-5" else (LIST_IN, LIST_OUT)
        out_c = ot * orr / 1e6
        floor = it * ir * 0.10 / 1e6 + out_c
        ceil = it * ir / 1e6 + out_c
        est = it * (eff or ir) * (ir / LIST_IN) / 1e6 + out_c if eff else (floor + ceil) / 2
        w.writerow([r.get("account_name"), m, it, ot, r.get("cost_usd"),
                    round(floor, 4), round(est, 4), round(ceil, 4), r.get("created_at")])
print("wrote sweep_cost_real.csv")

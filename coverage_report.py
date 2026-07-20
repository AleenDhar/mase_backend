"""Separate 'no Avoma coverage' from 'coverage discovered but not read'.

A score with calls_read=0 looks identical to a score with calls_read=20 in the drawer.
This asks, per deal: how many sessions did the never-miss engine DISCOVER, how many were
READ, and for each omission — WHY (not_recorded / no content captured / clipped / budget).

  discovered == read              -> full coverage
  omitted, reason=not_recorded    -> genuinely no content (Avoma never joined) — honest gap
  omitted, reason=no content      -> session exists, transcript empty — honest gap
  discovered > read + omitted     -> SILENT CLIP: sessions vanished with no reason. BUG.
"""
import csv, sys, warnings, collections
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

DEALS = [("Robert Bosch", "006P700000PlMpu"), ("Temasek", "006P700000BV2eA"),
         ("Techtronic", "006P700000GWfrf"), ("SAMI", "006P700000RD9Ir"),
         ("Wheelson", "006P700000VlPdp"), ("Mair Group", "006P700000PtQGP"),
         ("MTR", "006P700000KTTO5"), ("Khansaheb", "006P700000LtIUv"),
         ("HAECO", "006P700000NwbBd"), ("Globe Telecom", "006P7000008hZHF"),
         ("Gamuda", "006P700000Q15OU"), ("Cebu Pacific", "0066700000wdNe1"),
         ("Bandhan Bank", "006P700000H55TV"), ("Arabian Industries", "006P700000QvP7Z"),
         ("ACEN", "006P700000DkWgX"), ("Etex Group", "006P700000UGPE5"),
         ("NORTHPORT", "006P700000QFJwD")]

SEL = ("scores:record->ai->deal_scores,cov:record->evidence_coverage,"
       "studio:record->ai->scoring_studio")


def bucket(reason):
    r = (reason or "").lower()
    if "not_recorded" in r or "not recorded" in r:
        return "not_recorded"
    if "no content" in r or "no recorded" in r or "gap" in r:
        return "no_content"
    return "other"


print("=" * 112)
print(f"{'deal':20}{'win':>6}{'mom':>6}{'eng':>7}{'disc':>6}{'read':>6}{'omit':>6}"
      f"{'unexplained':>12}  verdict")
print("=" * 112)
rows_out = []
reasons_tally = collections.Counter()
for lbl, oid in DEALS:
    try:
        r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                         headers=SH, verify=False, timeout=(10, 90)).json()
    except Exception as e:
        print(f"{lbl:20}  read error {type(e).__name__}"); continue
    if not r:
        print(f"{lbl:20}  NO ROW"); continue
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    cov = r.get("cov") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    disc = cov.get("calls_discovered")
    read = cov.get("calls_read")
    om = cov.get("calls_omitted") or []
    def _reason(o):
        return o.get("reason") if isinstance(o, dict) else str(o)
    def _date(o):
        return o.get("date") if isinstance(o, dict) else ""
    for o in om:
        reasons_tally[bucket(_reason(o))] += 1
    unexplained = None
    if isinstance(disc, int) and isinstance(read, int):
        unexplained = disc - read - len(om)

    if read in (0, None):
        verdict = "SCORED BLIND — no call evidence"
    elif unexplained and unexplained > 0:
        verdict = f"SILENT CLIP — {unexplained} session(s) vanished"
    elif isinstance(read, int) and read <= 2:
        verdict = "thin (verify coverage is genuinely absent)"
    else:
        verdict = "ok"
    print(f"{lbl:20}{str(hl.get('win_position')):>6}{str(hl.get('deal_momentum')):>6}"
          f"{'v' + str(sv.get('win')):>7}{str(disc):>6}{str(read):>6}{len(om):>6}"
          f"{str(unexplained):>12}  {verdict}")
    rows_out.append([lbl, oid, hl.get("win_position"), hl.get("deal_momentum"), sv.get("win"),
                     disc, read, len(om), unexplained, verdict,
                     " | ".join(f"{_date(o)}:{_reason(o)}" for o in om)[:500]])

print("\nomission reasons across the set:", dict(reasons_tally))
print("\nKEY  not_recorded/no_content = honest gap (Avoma never captured it).")
print("     unexplained > 0          = sessions discovered, not read, no reason given -> READ BUG.")

with open("coverage_report.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "win", "momentum", "win_engine", "calls_discovered",
                "calls_read", "calls_omitted", "unexplained_gap", "verdict", "omission_detail"])
    w.writerows(rows_out)
print("\nwrote coverage_report.csv")

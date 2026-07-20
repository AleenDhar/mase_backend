"""Authoritative inventory: every opp touched this session, live from deal_records.

Reports current win/mom/commit/risk, read label, engine version, factor_source, calls_read,
last-updated, and the win+mom reason bullets. Groups by engine version so it's clear which
carry the newest v10.8 logic vs the earlier v10.7 pass, and flags null/degraded/thin records.
"""
import csv, sys, warnings, datetime
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

# Every opp touched this session (label, opp_id, which run first scored it)
DEALS = [
    ("Robert Bosch",       "006P700000PlMpu"), ("SAMI",              "006P700000RD9Ir"),
    ("Allstate",           "006P7000006uKrq"), ("NORTHPORT",         "006P700000QFJwD"),
    ("Domino's Pizza",     "006P700000X6hvK"), ("Greencore",         "006P700000WeRX8"),
    ("SARS",               "006P700000UZv8c"), ("Etex Group",        "006P700000UGPE5"),
    ("Temasek",            "006P700000BV2eA"), ("Techtronic",        "006P700000GWfrf"),
    ("Wheelson/Mumtalakat", "006P700000VlPdp"), ("Mair Group",       "006P700000PtQGP"),
    ("MTR Corporation",    "006P700000KTTO5"), ("Khansaheb",         "006P700000LtIUv"),
    ("HAECO",              "006P700000NwbBd"), ("Globe Telecom",     "006P7000008hZHF"),
    ("Gamuda",             "006P700000Q15OU"), ("Cebu Pacific Air",  "0066700000wdNe1"),
    ("Bandhan Bank",       "006P700000H55TV"), ("Arabian Industries", "006P700000QvP7Z"),
    ("ACEN",               "006P700000DkWgX"),
]
SEL = ("account_name,stage,close_date,amount,updated_at,scores:record->ai->deal_scores,"
       "studio:record->ai->scoring_studio,cov:record->evidence_coverage")
NBUL = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def fetch(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    return r[0] if isinstance(r, list) and r else None


rows = []
for lbl, oid in DEALS:
    try:
        r = fetch(oid)
    except Exception as e:
        rows.append({"lbl": lbl, "oid": oid, "err": type(e).__name__}); continue
    if not r:
        rows.append({"lbl": lbl, "oid": oid, "err": "NO ROW"}); continue
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    rows.append({
        "lbl": lbl, "oid": oid, "acct": r.get("account_name"), "stage": r.get("stage"),
        "upd": r.get("updated_at"), "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
        "commit": hl.get("customer_commitment"), "risk": hl.get("deal_risk"),
        "read": hl.get("read"), "src": ds.get("factor_source"), "engine": sv.get("win"),
        "degraded": ds.get("scoring_degraded"),
        "calls": (r.get("cov") or {}).get("calls_read"),
        "reasons": ds.get("ai_reasons") or {},
        "scored": ds and hl.get("win_position") is not None,
    })

scored = [r for r in rows if r.get("scored")]
v108 = [r for r in scored if str(r.get("engine")) == "10.8"]
v107 = [r for r in scored if str(r.get("engine")) == "10.7"]
other = [r for r in scored if str(r.get("engine")) not in ("10.8", "10.7")]
blank = [r for r in rows if not r.get("scored")]

print("=" * 104)
print(f"SCORED INVENTORY — {len(scored)}/{len(DEALS)} opportunities carry a live governed score")
print(f"   v10.8 (newest logic): {len(v108)}   ·   v10.7 (earlier today): {len(v107)}"
      f"   ·   other: {len(other)}   ·   blank/damaged: {len(blank)}")
print("=" * 104)
print(f"{'deal':21}{'stage':20}{'WIN':>6}{'MOM':>6}{'COM':>6}{'RSK':>6}  {'read':<16}"
      f"{'eng':>6}{'cal':>5}  updated")
print("-" * 104)
for grp, name in ((v108, "── v10.8 ──"), (v107, "── v10.7 ──"), (other, "── other ──")):
    if not grp:
        continue
    print(name)
    for r in sorted(grp, key=lambda x: -(x["win"] or 0)):
        flag = " ⚠THIN" if (r["calls"] or 0) <= 2 else ""
        print(f"  {r['lbl']:19}{str(r['stage'])[:19]:20}{str(r['win']):>6}{str(r['mom']):>6}"
              f"{str(r['commit']):>6}{str(r['risk']):>6}  {str(r['read'])[:15]:<16}"
              f"{'v'+str(r['engine']):>6}{str(r['calls']):>5}  {str(r['upd'])[:16]}{flag}")
if blank:
    print("── blank / needs re-sweep ──")
    for r in blank:
        print(f"  {r['lbl']:19}{'':20}{'—':>6}  {r.get('err','deal_scores null')}")

print("\n" + "=" * 104)
print("REASONS (top win + momentum drivers per deal)")
print("=" * 104)
for r in sorted(scored, key=lambda x: (str(x['engine']) != "10.8", -(x["win"] or 0))):
    print(f"\n### {r['lbl']} — {r['acct']}  ·  WIN {r['win']} / MOM {r['mom']} / "
          f"COMMIT {r['commit']} / RISK {r['risk']}  ·  {r['read']}  ·  v{r['engine']} · {r['calls']} calls")
    for key, tag in (("win_position", "WIN"), ("deal_momentum", "MOM")):
        for b in (r["reasons"].get(key) or [])[:NBUL]:
            print(f"   {tag} [{b.get('tone')}] {b.get('text')}")

with open("scored_inventory.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "account", "stage", "win", "momentum", "commitment", "risk",
                "read", "engine", "factor_source", "calls_read", "updated_at",
                "win_reasons", "momentum_reasons", "commitment_reasons", "risk_reasons"])
    for r in rows:
        if not r.get("scored"):
            w.writerow([r["lbl"], r["oid"], "", "", "", "", "", "", r.get("err", "blank")] + [""] * 8)
            continue
        rs = r["reasons"]
        j = lambda k: " || ".join(f"[{x.get('tone')}] {x.get('text')}" for x in (rs.get(k) or []))
        w.writerow([r["lbl"], r["oid"], r["acct"], r["stage"], r["win"], r["mom"], r["commit"],
                    r["risk"], r["read"], r["engine"], r["src"], r["calls"], r["upd"],
                    j("win_position"), j("deal_momentum"), j("customer_commitment"), j("deal_risk")])
print("\nwrote scored_inventory.csv (full 4-category reasons for every deal)")

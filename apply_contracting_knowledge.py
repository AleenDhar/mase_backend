"""Append the distilled Zycus contracting glossary to the LIVE mase_deal_sweep
prompt (Supabase). Backs up outside the repo; inserts before '## 3. The North Star';
verifies additive; reversible. Run with --apply to write."""
import sys, os, datetime, requests, urllib3
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ANCHOR = "## 3. The North Star"
BLOCK = (
    "## 2.9 Zycus contracting & terminology (NEW-BUSINESS deals) — read contracting-stage deals with this\n\n"
    "How Zycus goes from \"we won\" to \"we can deliver\". Use this to interpret Vendor Selected / "
    "Negotiation / Contract In Progress / Contract Signed / PO Received deals. NEW-BUSINESS only "
    "(renewals / change-requests / cross-sell / upsell are lighter; a single-module Certinal-only "
    "deal is Order Form + SOW with NO MSA — do not weight it as full-suite).\n"
    "- Contracting is a 6-phase relay: (1) Commercials locked — BAFO -> LOI, term/payment "
    "milestones; (2) Paper drafted — MSA, Order Form 1/2, SOW (\"whose paper?\" Zycus SaaS vs buyer "
    "standard); (3) Legal & redlines — MSA redlines, jurisdiction / board resolution, "
    "termination-for-convenience (T4C); (4) Infosec/compliance/onboarding — RUNS PARALLEL to legal; "
    "(5) Signature — internal legal cover -> e-sign (DocuSign/Certinal) -> dual signatories; "
    "(6) PO & handoff — PO -> Sales Order -> licence invoice; signed SOW -> kickoff.\n"
    "- CONTRACT IN PROGRESS IS NOT ONE GATE. It holds FOUR independent tracks that resolve "
    "separately: legal (MSA/jurisdiction/T4C), infosec+compliance (SOC 1/2 + security questionnaire, "
    "DPA / GDPR+TOM incl. the Zycus-India sub-processor disclosure, AI-governance / AIGC board for "
    "AI-module deals), supplier-onboarding (vendor registration / risk portals like Aravo, "
    "Venminder), and signature. When a Contract-In-Progress deal stalls, identify WHICH gate — do "
    "not read it as generic stalling.\n"
    "- THE SOW IS THE CHOKE POINT AND THE SIGNATURE PREDICTOR. Buyers routinely agree the MSA + "
    "Order Form but WILL NOT SIGN until the SOW is agreed (the SOW is signed SEPARATELY by the AVP "
    "Global Delivery, not bundled with the MSA). So \"won't sign until the SOW\" is NORMAL, not a "
    "red flag — track SOW status to forecast the close, and treat a signed/agreed SOW as the real "
    "signal that signature is imminent.\n"
    "- PO IS REGION-CONDITIONAL. DACH / APAC / emerging markets issue a PO that gates invoicing; "
    "much of W. Europe and the US invoice DIRECTLY with NO PO (an \"invoice details form\"). A "
    "MISSING PO in Europe is NORMAL — never flag \"no PO\" as a problem or a missing step there. "
    "\"PO Received\" is a real SF stage but region-optional, not a universal gate to Closed-Won.\n"
    "- Signatories: the RVP signs the MSA + Order Form 1; the AVP Global Delivery signs the SOW.\n"
    "- Glossary: BAFO (Best & Final Offer), LOI (Letter of Intent), MSA (Master Service Agreement), "
    "OF1/OF2 (Order Form — primary / add-on e.g. Certinal), SOW (Statement of Work), DPA / GDPR+TOM "
    "(data-processing addendum), SOC 1/2 (infosec evidence pack), T4C (termination-for-convenience), "
    "framework + call-off (Nordics alternative to MSA+OF), Zycus SO Form (internal sales order handed "
    "to Finance), AIGC / AI-governance board (buyer AI-compatibility board), RTO/RPO (recovery time / "
    "point objectives), NDA, supplier onboarding / vendor master.\n\n")


def main():
    dry = "--apply" not in sys.argv
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    cur = requests.get(f"{base}/rest/v1/jarvis_settings",
                       params={"id": "eq.mase_deal_sweep", "select": "system_prompt"},
                       headers=h, verify=False, timeout=40).json()[0]["system_prompt"]
    print(f"[read] prompt {len(cur)} chars")
    if "Zycus contracting & terminology" in cur:
        print("!! contracting block already present — abort (idempotent)."); return
    n = cur.count(ANCHOR)
    if n != 1:
        print(f"!! anchor {ANCHOR!r} appears {n}x — expected 1. ABORT."); return
    new = cur.replace(ANCHOR, BLOCK + ANCHOR, 1)
    assert new.replace(BLOCK, "", 1) == cur, "not purely additive"
    assert new.count(ANCHOR) == 1
    print(f"[verify] additive OK (+{len(BLOCK)} chars)")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bk = os.path.join(os.path.expanduser("~"), f"mase_deal_sweep_prompt_backup_{stamp}.md")
    open(bk, "w", encoding="utf-8").write(cur)
    print(f"[backup] {bk}")
    if dry:
        print("\n[DRY RUN] re-run with --apply to write."); return
    r = requests.post(f"{base}/rest/v1/jarvis_settings", params={"on_conflict": "id"},
                      headers={**h, "Content-Type": "application/json",
                               "Prefer": "resolution=merge-duplicates,return=minimal"},
                      json={"id": "mase_deal_sweep", "system_prompt": new,
                            "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                      verify=False, timeout=60)
    if r.status_code >= 300:
        print("!! WRITE FAILED", r.status_code, r.text[:300]); return
    back = requests.get(f"{base}/rest/v1/jarvis_settings",
                        params={"id": "eq.mase_deal_sweep", "select": "system_prompt"},
                        headers=h, verify=False, timeout=40).json()[0]["system_prompt"]
    print(f"[write] OK. prompt now {len(back)} chars; block present: {'Zycus contracting & terminology' in back}")


if __name__ == "__main__":
    main()

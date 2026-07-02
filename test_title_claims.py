"""Standalone test for the new title-claim verifier (no network)."""
import sys
import deal_engine_validation as V
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def n(s, ct):
    out, fixes = V._neutralise_title_claims(s, ct, allow=set(ct.keys()))
    return out, fixes


AUSTRIAN = {  # real Austrian Post SF contact titles (none are CFO; no Flandorfer)
    "engelbert pölki": "Manager Strategic Projects IT & Supply Chain",
    "karin eppich": "Purchasing processes and systems",
    "koller melissa": "Head of Purchasing Procure2Pay",
}
CASES = [
    # (text, contact_titles, expect_fixed>0, must_not_contain, must_contain)
    ("Broker a connect; the economic-buyer CFO Flandorfer has never engaged in 14 months.",
     AUSTRIAN, True, ["CFO Flandorfer", "economic-buyer CFO"], ["Flandorfer"]),
    ("Present AI iSaaS to the CIO to prove 30 vs 170 days.",
     AUSTRIAN, False, [], ["CIO"]),  # no name after CIO -> untouched
    ("Put a Zycus exec in front of EB/CFO Jason Chan to anchor ROI.",
     {"jason chan": "Controller and CFO"}, False, [], ["CFO Jason Chan"]),  # verified -> kept
    ("Reach CFO Pölki for budget sign-off.",
     AUSTRIAN, True, ["CFO Pölki"], ["Pölki"]),  # Pölki is not finance -> drop CFO
    ("Escalate to the decision maker workshop next week.",
     AUSTRIAN, False, [], ["decision maker workshop"]),  # no capitalised name -> untouched
    ("Align with Florence Tinsley-Roy (CPO) on Merlin scope.",
     {"florence tinsley-roy": "Chief Procurement Officer"}, False, [], ["(CPO)"]),  # verified -> kept
    ("Get the CEO Walter Oblin and CTO Michael Niessl into the room.",
     {"walter oblin": "Generaldirektor / CEO", "michael niessl": "CTO"},
     False, [], ["CEO Walter Oblin", "CTO Michael Niessl"]),  # both verified -> kept
]

ok = True
for i, (text, ct, expect_fixed, must_not, must) in enumerate(CASES, 1):
    out, fixes = n(text, ct)
    fixed_ok = (fixes > 0) == expect_fixed
    nc_ok = all(m not in out for m in must_not)
    c_ok = all(m in out for m in must)
    passed = fixed_ok and nc_ok and c_ok
    ok = ok and passed
    print(f"[{'PASS' if passed else 'FAIL'}] case {i} (fixes={fixes})")
    print(f"    in : {text}")
    print(f"    out: {out}")
    if not passed:
        if not fixed_ok:
            print(f"    !! expected fixed={expect_fixed} got fixes={fixes}")
        for m in must_not:
            if m in out:
                print(f"    !! should NOT contain: {m!r}")
        for m in must:
            if m not in out:
                print(f"    !! should contain: {m!r}")

print("\nALL PASS" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)

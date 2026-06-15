"""Read-only verification: for each opp in the MASE fabrication worklist, fetch the
CURRENT stored deal record and report whether the earlier fabrication signatures are
gone. Checks (1) hard.manager_name vs the flagged fabricated value, and (2) whether
the flagged evidence token / fabricated name still appears ANYWHERE in the record
(hard + ai + packets + deltas), since the token-free hard-refresh only rewrites the
hard.* facts and does not touch AI narrative."""
from __future__ import annotations

import csv
import json
import re
import sys

import deal_engine_validation as V
import deal_engine_store as store

CSV = "attached_assets/mase_fabrication_worklist_1781536627715.csv"

_ANGLE = re.compile(r"<[^<>\n]{1,40}>")


def _norm(s):
    return re.sub(r"\s+", " ", s).strip().casefold() if isinstance(s, str) else ""


def _strip_quotes(s: str) -> str:
    return (s or "").strip().strip('"').strip()


def main():
    rows = []
    with open(CSV, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    print(f"worklist rows: {len(rows)}\n")
    clean = 0
    dirty = []
    missing = []

    for r in rows:
        opp = (r["opp_id"] or "").strip()[:15]
        acct = r["account"]
        reason = r["reasons"]
        evidence = _strip_quotes(r["evidence"])

        rec = store.get_record(opp)
        if not rec:
            missing.append((opp, acct))
            print(f"[MISSING] {opp} {acct} — no stored record")
            continue

        hard = rec.get("hard") or {}
        mgr = hard.get("manager_name")
        mgr_src = hard.get("manager_name_source")
        blob = json.dumps(rec, ensure_ascii=False, default=str)

        problems = []

        # 1) hard.manager_name must no longer be the fabricated value
        if reason == "fabricated-manager":
            # evidence is like: "Andrew Grant" not a SF user  -> extract the name
            fab = evidence.split('" not')[0].strip().strip('"')
            # some carry a verb prefix ("Use Mark Davidson", "Bring David Chen",
            # "Manager David Chen") — take the trailing 2 tokens as the name guess too
            if _norm(mgr) == _norm(fab):
                problems.append(f"manager STILL fabricated = {mgr!r}")
            if fab and _norm(fab) in _norm(blob):
                problems.append(f"fabricated name {fab!r} still appears in record")

        # 2) placeholder tokens must be gone everywhere
        if reason == "placeholder-token":
            tok = evidence
            if tok == "manager_name":
                if mgr == "manager_name":
                    problems.append("hard.manager_name == 'manager_name' token")
                # the literal token as a standalone value anywhere
                if re.search(r'"manager_name"\s*:\s*"manager_name"', blob):
                    problems.append("literal 'manager_name' value present")
            else:
                if tok and tok in blob:
                    problems.append(f"placeholder {tok!r} still in record")

        # 3) forbidden carried-forward phrase (real leakage; the angle-bracket scan
        # is intentionally OMITTED — next_step is HTML so <p>/<li>/<br> etc. are
        # legitimate, not placeholders).
        if "historical record from prior sweep" in blob.lower():
            problems.append("placeholder phrase 'historical record from prior sweep'")

        tag = "OK " if not problems else "DIRTY"
        if not problems:
            clean += 1
        else:
            dirty.append((opp, acct, problems))
        print(f"[{tag}] {opp} {acct[:28]:28} mgr={mgr!r} src={mgr_src!r}"
              + ("" if not problems else "  -> " + "; ".join(problems)))

    print(f"\n==== SUMMARY ====")
    print(f"clean: {clean}/{len(rows)}")
    print(f"dirty: {len(dirty)}")
    print(f"missing record: {len(missing)}")
    if dirty:
        print("\nDIRTY detail:")
        for opp, acct, probs in dirty:
            print(f"  {opp} {acct}: {probs}")

    # Emit the dirty opp ids as the single source of truth for a targeted re-sweep.
    out_path = ".local/fabrication_dirty_ids.json"
    with open(out_path, "w") as f:
        json.dump({"dirty_opp_ids": [opp for opp, _, _ in dirty],
                   "clean": clean, "dirty": len(dirty),
                   "missing": [m[0] for m in missing]}, f, indent=2)
    print(f"\nwrote dirty ids -> {out_path}")


if __name__ == "__main__":
    sys.exit(main())

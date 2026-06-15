"""Deterministic, surgical scrub of the 4 deal records the AI re-sweep could not
clean, because the offending text lives in carried-forward living-memory
(packets[]/deltas[]) free-text that the sweep MERGES forward and the
anti-fabrication gate never rewrites.

This is NOT an AI pass: it applies a small, explicit set of string transforms to
exactly the four flagged records, then re-reads and asserts the flagged token is
gone before writing. Idempotent — re-running after a clean record is a no-op.

Targets (from scripts/verify_fabrication_worklist.py):
  006P700000KCwMV ALTRAD              fabricated name 'Andrew Grant'
  006P700000KgxjK WorkSafe Victoria   fabricated name 'James Patterson'
  006P700000OaMiX Foundever           placeholder token '[proposal]'
  006P700000PWx3N Kisco Senior Living placeholder template incl. '[Vendor X]'

Usage:
  python3 scripts/scrub_fabrication_worklist.py            # dry-run (no writes)
  python3 scripts/scrub_fabrication_worklist.py --apply    # write the cleaned records
"""
from __future__ import annotations

import json
import re
import sys

import deal_engine_store as store

# Per-opp transforms. Each is (kind, pattern, replacement):
#   "re"  -> re.sub(pattern, replacement, s, flags=I|S)
#   "lit" -> s.replace(pattern, replacement)
SCRUBS: dict[str, list[tuple[str, str, str]]] = {
    # ALTRAD: drop the fabricated manager parenthetical; keep the "AG" initials
    # and the real commitment text intact.
    "006P700000KCwMV": [
        ("re", r"\s*\([^)]*Andrew Grant[^)]*\)", ""),
        ("re", r"(?i)\bAndrew Grant\b", ""),
    ],
    # WorkSafe Victoria: drop the fabricated manager parenthetical.
    "006P700000KgxjK": [
        ("re", r"\s*\([^)]*James Patterson[^)]*\)", ""),
        ("re", r"(?i)\bJames Patterson\b", ""),
    ],
    # Foundever: the bracketed word inside a quoted Next_Step is a placeholder;
    # debracket it so the sentence reads naturally without the token.
    "006P700000OaMiX": [
        ("lit", "[proposal]", "proposal"),
    ],
    # Kisco: the trailing quote is a pure fill-in-the-blank coaching template
    # ([Vendor X]/[reason Y]/[deliver Z]/[Z]/[reference customer...]). Remove the
    # whole template quote, keeping the legitimate strategic lead-in.
    "006P700000PWx3N": [
        ("re", r":\s*'You chose \[Vendor X\].*?senior living\]\.'", "."),
        ("lit", "[Vendor X]", ""),
    ],
}

# Post-scrub assertion: the flagged token/name must be gone from the record blob.
CHECK: dict[str, tuple[str, str]] = {
    "006P700000KCwMV": ("name", "Andrew Grant"),
    "006P700000KgxjK": ("name", "James Patterson"),
    "006P700000OaMiX": ("tok", "[proposal]"),
    "006P700000PWx3N": ("tok", "[Vendor X]"),
}

ACCT = {
    "006P700000KCwMV": "ALTRAD",
    "006P700000KgxjK": "WorkSafe Victoria",
    "006P700000OaMiX": "Foundever",
    "006P700000PWx3N": "Kisco Senior Living",
}


def _tidy(s: str) -> str:
    """Clean up artifacts a removal can leave behind (only run on changed
    strings): collapse runs of spaces, fix orphaned punctuation/parens."""
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = s.replace(" ,", ",").replace(" .", ".").replace(" ;", ";").replace(" :", ":")
    s = s.replace("( ", "(").replace(" )", ")")
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s


def _apply(s: str, rules: list[tuple[str, str, str]]) -> str:
    out = s
    for kind, pat, rep in rules:
        if kind == "re":
            out = re.sub(pat, rep, out, flags=re.I | re.S)
        else:
            out = out.replace(pat, rep)
    if out != s:
        out = _tidy(out)
    return out


def _walk(node, rules, path, changes):
    """Recursively transform every string value; record (path, before, after)."""
    if isinstance(node, str):
        new = _apply(node, rules)
        if new != node:
            changes.append((path, node, new))
        return new
    if isinstance(node, dict):
        return {k: _walk(v, rules, f"{path}.{k}", changes) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, rules, f"{path}[{i}]", changes) for i, v in enumerate(node)]
    return node


def _present(token: str, kind: str, blob: str) -> bool:
    return (token.casefold() in blob.casefold()) if kind == "name" else (token in blob)


def main(apply: bool) -> int:
    print(f"MODE: {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}\n")
    any_fail = False
    for opp, rules in SCRUBS.items():
        acct = ACCT[opp]
        rec = store.get_record(opp)  # fresh read right before write
        if not rec:
            print(f"[MISS] {opp} {acct}: no record"); any_fail = True; continue

        kind, token = CHECK[opp]
        before_blob = json.dumps(rec, ensure_ascii=False, default=str)
        if not _present(token, kind, before_blob):
            print(f"[SKIP] {opp} {acct}: already clean of {token!r} (no-op)")
            continue

        changes: list = []
        cleaned = _walk(rec, rules, "rec", changes)
        after_blob = json.dumps(cleaned, ensure_ascii=False, default=str)

        print(f"[{opp}] {acct}: {len(changes)} string(s) changed; {token!r} -> "
              f"{'GONE' if not _present(token, kind, after_blob) else 'STILL PRESENT'}")
        for path, b, a in changes:
            bs = b if len(b) <= 240 else b[:240] + "…"
            as_ = a if len(a) <= 240 else a[:240] + "…"
            print(f"   PATH {path}")
            print(f"     -  {bs!r}")
            print(f"     +  {as_!r}")

        if _present(token, kind, after_blob):
            print(f"   !! token still present after scrub — NOT writing {opp}")
            any_fail = True
            continue

        if apply:
            store.upsert_record(cleaned)
            print("   -> WROTE cleaned record")
        print()

    print("\nDONE." + ("" if apply else "  (dry-run — re-run with --apply to write)"))
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv[1:]))

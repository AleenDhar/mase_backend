"""One-time, no-LLM scrub of the durable packet store so the implicit_requirements
backfill survives future sweeps.

Why this exists
---------------
`scripts/backfill_implicit_requirements.py` cleaned each record's `ai.*`
projection (cleaned implicit list + routed items), but ~121 deals also carry a
durable `packets[]` store. The sweep regenerates the packet-backed `ai.*` lists
(implicit/explicit requirements, open_deliverables, ...) from `packets[]` every
run via `deal_engine_packets.project_into_ai`, and durable packets absent from a
sweep are RETAINED (dormant, still projected). So without this scrub the old
implicit noise would reappear on each deal's next sweep.

What it does (deterministic, no model call)
-------------------------------------------
For every record that has packets, it rebuilds ONLY the `requirement` and
`commitment` packets from the ALREADY-CLEANED `ai.*`:
  * candidates are derived with the library's own `extract_candidates`,
  * a candidate whose key matches an existing packet keeps that packet's history
    (first_seen / status / history); its value is refreshed,
  * a candidate with no matching existing packet becomes a fresh active packet,
  * an existing requirement/commitment packet NOT reflected in the cleaned `ai.*`
    is DROPPED (this is the deliberate scrub of the mis-categorised implicit
    noise + routed-away items).
All other packet types (stakeholder, risk, competitor, hygiene, champion,
product_scope) and the change-feed `deltas[]` are left untouched. Finally it
re-projects `ai` from the new packet set so `ai` and `packets` stay consistent
(non-packet sections like recommended_moves / north_star are preserved).

Idempotent and re-runnable: a second pass finds the packets already matching the
cleaned `ai`, so nothing is dropped and nothing changes. Safe to re-run if the
process is killed mid-way.
"""
import sys
import json
import datetime

sys.path.insert(0, ".")

import deal_engine_store as store  # noqa: E402
import deal_engine_packets as pk  # noqa: E402

AFFECTED = {"requirement", "commitment"}


def _today() -> str:
    return datetime.date.today().isoformat()


def rebuild_record(rec: dict, today: str):
    """Return (new_packets, new_ai, n_dropped) for a single record."""
    existing = rec.get("packets") or []
    ai = rec.get("ai") or {}
    hard = rec.get("hard") or {}

    keep = [p for p in existing if p.get("type") not in AFFECTED]
    old_affected = {p["key"]: dict(p) for p in existing
                    if p.get("type") in AFFECTED and p.get("key")}

    cands = [c for c in pk.extract_candidates(ai, hard)
             if c.get("type") in AFFECTED]

    rebuilt = []
    seen = set()
    for c in cands:
        key = pk._key_of(c)
        if not key or key in seen:
            continue
        seen.add(key)
        raw = c.get("value")
        cval = raw if isinstance(raw, dict) else {"value": raw}
        if key in old_affected:
            p = old_affected[key]
            p["value"] = cval
            p["last_confirmed"] = today
            if p.get("status") == "dormant":
                p["status"] = "active"
            rebuilt.append(p)
        else:
            rebuilt.append({
                "key": key,
                "type": c.get("type"),
                "subject": c.get("subject") or key.split(":", 1)[-1],
                "value": cval,
                "status": "active",
                "first_seen": today,
                "last_confirmed": today,
                "last_updated": today,
                "source": c.get("source"),
                "confidence": c.get("confidence"),
                "evidence": c.get("evidence"),
                "history": [],
            })

    n_old_affected = len(old_affected)
    n_dropped = n_old_affected - sum(1 for c_key in seen if c_key in old_affected)

    new_packets = keep + rebuilt
    new_ai = pk.project_into_ai(ai, new_packets)
    return new_packets, new_ai, n_dropped


def main():
    # IMPORTANT (dup-safety): read the actual PRIMARY-KEY opp_id column, NOT just
    # the record jsonb. Some rows carry an 18-char opp_id inside the jsonb while
    # the canonical row is keyed on the 15-char PK. upsert_record() keys on
    # record["opp_id"], so writing the raw jsonb would INSERT a second row under
    # the 18-char key. We force record["opp_id"] = PK before any write so the
    # upsert always merges into the canonical 15-char row.
    rows = store._select(store.T_RECORDS, select="opp_id,record")
    print(f"rows={len(rows)}", flush=True)

    today = _today()
    scrubbed = key_fixed = dropped_total = 0
    for i, row in enumerate(rows, 1):
        pk = (row.get("opp_id") or "").strip()
        rec = row.get("record") or {}
        if not pk or not rec:
            continue

        need_upsert = False
        # 1) normalise the jsonb opp_id to the canonical PK (dup-safety).
        if rec.get("opp_id") != pk:
            rec["opp_id"] = pk
            key_fixed += 1
            need_upsert = True

        # 2) scrub the durable packet store (only if this deal has packets).
        if rec.get("packets"):
            new_packets, new_ai, n_dropped = rebuild_record(rec, today)
            before = json.dumps([rec.get("packets"), rec.get("ai")],
                                sort_keys=True, default=str)
            after = json.dumps([new_packets, new_ai], sort_keys=True, default=str)
            if before != after:
                rec["packets"] = new_packets
                rec["ai"] = new_ai
                scrubbed += 1
                dropped_total += max(n_dropped, 0)
                need_upsert = True

        if need_upsert:
            store.upsert_record(rec)
            if (scrubbed + key_fixed) % 20 == 0:
                print(f"  progress: scrubbed={scrubbed} key_fixed={key_fixed} "
                      f"(seen {i}/{len(rows)})", flush=True)

    print(f"DONE: scrubbed={scrubbed} key_fixed={key_fixed} "
          f"stale_affected_packets_dropped={dropped_total}", flush=True)


if __name__ == "__main__":
    main()

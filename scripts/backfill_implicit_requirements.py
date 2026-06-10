#!/usr/bin/env python3
"""One-time backfill: re-classify the `implicit_requirements` of every cached
deal record against the NEW rule (see prompts/deal_engine_sweep_system_prompt.md).

NEW rule (one-line test): explicit = "they asked for it"; implicit = "we promised
to give it". An implicit requirement is a CONCRETE deliverable WE (Zycus)
volunteered to provide that the prospect did NOT categorically request.

For each record we take its EXISTING `ai.implicit_requirements` items and ask
gpt-4o (no tools bound, so no MCP hang) to:
  - KEEP + re-phrase survivors as imperative deliverables we owe,
  - ROUTE mis-categorised items OUT of implicit into explicit_requirements /
    recommended_moves / open_deliverables,
  - DROP generic rapport / vague claims.
We then rewrite `ai.implicit_requirements`, append routed items (tagged
`_via=implicit_backfill`, de-duped) to the target arrays, and upsert the record.
critical / important / explicitRequirements / bestPractice are otherwise
untouched: they only RECEIVE items routed out of implicit.

Usage:
  python3 scripts/backfill_implicit_requirements.py --dry-run --limit 5
  python3 scripts/backfill_implicit_requirements.py --dry-run --opp-ids 006...,006...
  python3 scripts/backfill_implicit_requirements.py            # full live run
  python3 scripts/backfill_implicit_requirements.py --concurrency 8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deal_engine_store as store

MODEL = os.environ.get("DEAL_ENGINE_MODEL", "gpt-4o")
ROUTE_MARK = "implicit_backfill"

SYSTEM = """You re-classify the "implicit requirements" of ONE sales opportunity.

DEFINITION
An implicit requirement = a CONCRETE deliverable WE (the seller, Zycus) volunteered
to provide the prospect that they did NOT categorically request. We owe it because
we offered it.

KEEP an item in implicit ONLY when ALL THREE hold:
  (a) it is a specific artifact or action we offered UNPROMPTED (e.g. "share 2-3
      named CPG customer references", "send the SOC 2 Type II report", "provide a
      sample BRD", "set up a peer reference call", "share the integration
      architecture doc");
  (b) it is material to advancing the deal (the prospect is waiting on it, or it
      builds credibility, trust, or momentum);
  (c) it is grounded in a real seller statement (keep grounding_quote + date).

ROUTE everything else OUT of implicit:
  - the prospect categorically/explicitly asked for it  -> route_explicit
  - our own internal next-best-action to drive the deal -> route_critical
  - a commitment the PROSPECT made to us                -> route_important
  - generic rapport or a vague claim with no specific promise to provide proof
    ("we've done a lot of work in your space")          -> drop it (fluff)

PHRASING for each kept implicit `inferred_need`: write it as the deliverable WE
owe, imperative, naming the specific artifact and (if known) the recipient.
  GOOD: "Share the 3 named CPG customer references we offered Darren on 12 May."
  BAD : "Customer may want to see references."
  GOOD: "Send the SOC 2 Type II report we promised IT security."
  BAD : "Security is likely a consideration."
Keep only real, deal-moving deliverables. When in doubt, drop it.

OUTPUT: emit ONLY a JSON object, no prose, no markdown fences:
{
  "implicit": [
    {"inferred_need": "<imperative deliverable WE owe>",
     "grounding_quote": "<verbatim seller statement, copied from input>",
     "date": "YYYY-MM-DD or null"}
  ],
  "route_explicit": [
    {"requirement": "<what the prospect asked for>", "said_by": "<prospect or null>",
     "date": "YYYY-MM-DD or null", "quote": "<grounding quote>"}
  ],
  "route_important": [
    {"who": "Buyer", "commitment": "<what the prospect committed to>",
     "date": "YYYY-MM-DD or null", "due": null}
  ],
  "route_critical": [
    {"action": "<our internal next move>", "trigger": "<grounding quote>",
     "trigger_date": "YYYY-MM-DD or null", "expected_effect": ""}
  ],
  "dropped": ["<original inferred_need text dropped as fluff>"]
}
Every input item must appear in exactly one of: implicit, route_explicit,
route_important, route_critical, dropped. Do not invent items not present in input.
Preserve grounding_quote / date verbatim from the matching input item.
"""


def _extract_json(text: str) -> dict:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b != -1 and b > a:
            return json.loads(s[a:b + 1])
        raise


def _norm(t) -> str:
    return re.sub(r"\s+", " ", str(t or "").strip().lower())


def _get_list(ai: dict, col: str) -> list:
    """Current items list for an ai column (handles the {items:[...]} wrapper or a
    bare list)."""
    v = ai.get(col)
    if isinstance(v, dict):
        items = v.get("items")
        return items if isinstance(items, list) else []
    return v if isinstance(v, list) else []


def _set_list(ai: dict, col: str, items: list) -> None:
    ai[col] = {"items": items}


def _append_routed(ai: dict, col: str, new_items: list, text_key: str) -> int:
    """Append routed items (tagged + de-duped by primary text) to an ai column.
    Returns how many were actually added."""
    if not new_items:
        return 0
    existing = _get_list(ai, col)
    seen = {_norm(it.get(text_key)) for it in existing if isinstance(it, dict)}
    added = 0
    for it in new_items:
        if not isinstance(it, dict):
            continue
        key = _norm(it.get(text_key))
        if not key or key in seen:
            continue
        it = {**it, "_via": ROUTE_MARK}
        existing.append(it)
        seen.add(key)
        added += 1
    if added:
        _set_list(ai, col, existing)
    return added


async def _classify(llm, rec: dict, items: list) -> dict:
    hard = rec.get("hard") or {}
    payload = {
        "account": hard.get("account_name"),
        "opportunity": hard.get("opp_name"),
        "owner": hard.get("owner_name"),
        "implicit_items": [
            {"inferred_need": it.get("inferred_need"),
             "grounding_quote": it.get("grounding_quote"),
             "date": it.get("date")}
            for it in items if isinstance(it, dict)
        ],
    }
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            "Re-classify the implicit_items for this opportunity per the rules. "
            "Return ONLY the JSON object.\n\n" + json.dumps(payload, default=str)},
    ]
    resp = await llm.ainvoke(msgs)
    return _extract_json(resp.content)


async def _process(llm, sem, rec: dict, dry: bool) -> dict:
    opp_id = rec.get("opp_id")
    hard = rec.get("hard") or {}
    ai = rec.get("ai") or {}
    before = _get_list(ai, "implicit_requirements")
    summary = {
        "opp_id": opp_id, "account": hard.get("account_name"),
        "before": len(before), "kept": 0, "dropped": 0,
        "route_explicit": 0, "route_important": 0, "route_critical": 0,
        "error": None, "changed": False,
    }
    if not before:
        return summary
    try:
        async with sem:
            out = await _classify(llm, rec, before)
    except Exception as e:  # noqa: BLE001
        summary["error"] = f"{type(e).__name__}: {e}"
        return summary

    kept = [x for x in (out.get("implicit") or []) if isinstance(x, dict)
            and (x.get("inferred_need") or "").strip()]
    summary["kept"] = len(kept)
    summary["dropped"] = len(out.get("dropped") or [])

    if dry:
        summary["route_explicit"] = len(out.get("route_explicit") or [])
        summary["route_important"] = len(out.get("route_important") or [])
        summary["route_critical"] = len(out.get("route_critical") or [])
        summary["changed"] = True
        summary["_preview"] = {
            "kept": [k.get("inferred_need") for k in kept],
            "dropped": out.get("dropped") or [],
            "route_explicit": [r.get("requirement") for r in (out.get("route_explicit") or [])],
            "route_important": [r.get("commitment") for r in (out.get("route_important") or [])],
            "route_critical": [r.get("action") for r in (out.get("route_critical") or [])],
        }
        return summary

    # Normalise routed items so they surface correctly in derive_todo:
    #  - open_deliverables only shows status open/overdue -> stamp "open";
    #  - recommended_moves ranks by `rank` and needs an owner for the card. Use a
    #    high rank so a backfilled move never displaces the agent's real rank-1.
    imp_items = [{**it, "status": (it.get("status") or "open")}
                 for it in (out.get("route_important") or []) if isinstance(it, dict)]
    crit_items = [{**it, "owner": (it.get("owner") or "Deal team"),
                   "rank": (it.get("rank") if it.get("rank") is not None else 99)}
                  for it in (out.get("route_critical") or []) if isinstance(it, dict)]

    ai = dict(ai)
    _set_list(ai, "implicit_requirements", kept)
    summary["route_explicit"] = _append_routed(
        ai, "explicit_requirements", out.get("route_explicit") or [], "requirement")
    summary["route_important"] = _append_routed(
        ai, "open_deliverables", imp_items, "commitment")
    summary["route_critical"] = _append_routed(
        ai, "recommended_moves", crit_items, "action")
    rec = {**rec, "ai": ai}
    await asyncio.get_running_loop().run_in_executor(None, store.upsert_record, rec)
    summary["changed"] = True
    return summary


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--opp-ids", default="")
    ap.add_argument("--owner", default="")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2

    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model=MODEL, temperature=0)

    records = await asyncio.to_thread(store.list_records, args.owner or None)
    if args.opp_ids:
        wanted = {i.strip()[:15] for i in args.opp_ids.split(",") if i.strip()}
        records = [r for r in records if str(r.get("opp_id") or "")[:15] in wanted]
    with_implicit = [r for r in records if _get_list(r.get("ai") or {}, "implicit_requirements")]
    if args.limit:
        with_implicit = with_implicit[:args.limit]

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"[{mode}] model={MODEL} concurrency={args.concurrency} | "
          f"records={len(records)} with_implicit={len(with_implicit)}", flush=True)

    sem = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(
        *[_process(llm, sem, r, args.dry_run) for r in with_implicit])

    tot = {"before": 0, "kept": 0, "dropped": 0, "route_explicit": 0,
           "route_important": 0, "route_critical": 0, "errors": 0, "changed": 0}
    for s in results:
        for k in ("before", "kept", "dropped", "route_explicit",
                  "route_important", "route_critical"):
            tot[k] += s.get(k, 0)
        if s.get("error"):
            tot["errors"] += 1
        if s.get("changed"):
            tot["changed"] += 1
        flag = " ERROR" if s.get("error") else ""
        print(f"  {s['opp_id']} | {str(s.get('account'))[:34]:34} | "
              f"in={s['before']:2} kept={s['kept']:2} drop={s['dropped']:2} "
              f"->exp={s['route_explicit']} ->imp={s['route_important']} "
              f"->crit={s['route_critical']}{flag}", flush=True)
        if s.get("error"):
            print(f"      {s['error']}", flush=True)
        if args.dry_run and s.get("_preview"):
            for k in ("kept", "dropped", "route_explicit", "route_important", "route_critical"):
                for t in s["_preview"].get(k, []):
                    print(f"      [{k}] {t}", flush=True)

    print(f"\n[{mode}] DONE deals_changed={tot['changed']} errors={tot['errors']} | "
          f"implicit before={tot['before']} kept={tot['kept']} dropped={tot['dropped']} | "
          f"routed exp={tot['route_explicit']} imp={tot['route_important']} "
          f"crit={tot['route_critical']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

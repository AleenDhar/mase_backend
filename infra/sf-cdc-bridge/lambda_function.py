"""
mase-sf-cdc-bridge — EventBridge -> MASE deal-engine trigger.

Receives Salesforce Change Data Capture events (Salesforce Event Relay ->
Amazon EventBridge partner bus) and decides, per affected Opportunity, whether
to (re-)analyze it by POSTing to the MASE backend:

    POST {MASE_TRIGGER_URL}   body {"opportunity_id": "<id>"}
    header Authorization: Bearer {DISPATCH_SECRET}

Decision per Opportunity id:
  - TRACKED (already in deal_records)        -> trigger (re-analysis).
  - NOT tracked, but the Opportunity event   -> ADOPT it: trigger anyway. The
    shows a Qualified-or-above OPEN stage        analysis upserts the record into
    (a new opp created that way, or a            deal_records, so triggering == adding
    stage-change into it)                        it to the tracked list.
  - otherwise                                 -> skip.

Stage gate: an untracked opp is adopted only when the CDC event carries a
StageName in QUALIFIED_PLUS_STAGES (open, Qualified or later). CDC only includes
StageName on CREATE or on a stage-change UPDATE, so this naturally fires only at
the two moments we want. Below-Qualified (Initial Interest, etc.) and closed/lost
stages are NOT in the set, so they are never adopted.

Entity -> Opportunity mapping:
  - OpportunityChangeEvent      -> the changed record id(s) (006...)
  - Task / Event / EmailMessage -> the related Opportunity via WhatId/RelatedToId
    (these never adopt — they only re-trigger an already-tracked opp, since the
    related opp's stage is not in the event).

Zero third-party deps so it runs on the stock python3.12 Lambda runtime.
"""
import json
import os
import urllib.request
import urllib.error

TRIGGER_URL = os.environ["MASE_TRIGGER_URL"]
DISPATCH_SECRET = os.environ.get("DISPATCH_SECRET", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

OPP_PREFIX = "006"  # Salesforce Opportunity key prefix

# Stages that are Qualified-or-above AND open. An untracked opp whose CDC event
# shows one of these is auto-adopted into the tracked list. Two stage taxonomies
# exist in the org (the main Zycus ladder + a numbered set); both are covered.
# Below-Qualified ("Initial Interest", "1. Generate interest", blank) and all
# closed/lost stages ("Closed Lost", "Qualified Out", "Omitted", "Closed") are
# intentionally absent -> never adopted. Update this set if the picklist changes.
QUALIFIED_PLUS_STAGES = {
    # main Zycus ladder (open, Qualified or later)
    "Qualified",
    "Shortlisted",
    "Formal Evaluation",
    "Vendor Selected",
    "Contract In Progress",
    "Contract Signed",
    "PO Received",
    # numbered ladder (2 = Qualified equivalent and later)
    "2. Solution Fitment",
    "3. Evaluation / POC",
    "4. Stakeholder Alignment",
    "5. Budget Approval",
    "6. Contract Negotiation",
}
_QUALIFIED_PLUS_LC = {s.strip().lower() for s in QUALIFIED_PLUS_STAGES}


def _is_qualified_plus(stage):
    return bool(stage) and stage.strip().lower() in _QUALIFIED_PLUS_LC


def _is_tracked(opp_id):
    """True if the opp is in the deal_records tracked set (15-char prefix match).
    Fail-open (True) if the check is unconfigured or errors."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return True
    prefix = opp_id[:15]
    url = SUPABASE_URL + "/rest/v1/deal_records?select=opp_id&limit=1&opp_id=like." + prefix + "*"
    req = urllib.request.Request(
        url, headers={"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read() or b"[]")
            return isinstance(rows, list) and len(rows) > 0
    except Exception as e:  # noqa: BLE001
        print("[tracked] check failed for %s: %s -> fail-open (trigger)" % (opp_id, e))
        return True


def _post_trigger(opp_id):
    body = json.dumps({"opportunity_id": opp_id, "source": "salesforce_trigger"}).encode("utf-8")
    req = urllib.request.Request(
        TRIGGER_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + DISPATCH_SECRET,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print("[trigger] opp=%s -> HTTP %s" % (opp_id, resp.status))
            return resp.status
    except urllib.error.HTTPError as e:
        print("[trigger] opp=%s -> HTTP %s: %s" % (opp_id, e.code, e.read()[:300]))
        return e.code
    except Exception as e:  # noqa: BLE001
        print("[trigger] opp=%s -> ERROR %s: %s" % (opp_id, type(e).__name__, e))
        return None


def _extract(detail):
    """From one CDC change event return (entity, change_type, opp_ids, stage).

    stage is the Opportunity's StageName when present in the payload (CREATE or a
    stage-change UPDATE); None otherwise. It is only meaningful for Opportunity
    events and drives the adopt decision.
    """
    payload = detail.get("payload") or detail
    header = payload.get("ChangeEventHeader", {}) or {}
    entity = header.get("entityName", "")
    change_type = header.get("changeType", "")
    record_ids = header.get("recordIds", []) or []

    opp_ids = set()
    stage = None
    if entity == "Opportunity":
        for rid in record_ids:
            if isinstance(rid, str) and rid.startswith(OPP_PREFIX):
                opp_ids.add(rid)
        sv = payload.get("StageName")
        if isinstance(sv, str):
            stage = sv
    else:
        # Task / Event / EmailMessage relate to an Opportunity via these fields.
        for field in ("WhatId", "RelatedToId"):
            v = payload.get(field)
            if isinstance(v, str) and v.startswith(OPP_PREFIX):
                opp_ids.add(v)
    return entity, change_type, opp_ids, stage


def handler(event, context):
    print("[event] detail-type=%s source=%s" % (event.get("detail-type"), event.get("source")))
    detail = event.get("detail", {}) or {}
    details = detail if isinstance(detail, list) else [detail]

    triggered, adopted, skipped = {}, [], []
    for d in details:
        entity, change_type, opp_ids, stage = _extract(d if isinstance(d, dict) else {})
        qualifies = entity == "Opportunity" and _is_qualified_plus(stage)
        print("[event] entity=%s changeType=%s stage=%r opp_ids=%s qualifies=%s"
              % (entity, change_type, stage, sorted(opp_ids), qualifies))
        for oid in opp_ids:
            if oid in triggered or oid in skipped:
                continue
            if _is_tracked(oid):
                triggered[oid] = _post_trigger(oid)
            elif qualifies:
                print("[adopt] opp=%s stage=%r is Qualified+ and untracked -> adding to "
                      "tracked list via trigger" % (oid, stage))
                triggered[oid] = _post_trigger(oid)
                adopted.append(oid)
            else:
                print("[skip] opp=%s untracked, not adopting (entity=%s stage=%r)"
                      % (oid, entity, stage))
                skipped.append(oid)

    if not triggered and not skipped:
        print("[event] no related Opportunity id found; nothing to do")
    return {
        "triggered": list(triggered.keys()),
        "adopted": adopted,
        "skipped": skipped,
        "results": triggered,
    }

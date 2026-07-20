"""Contract tests for the exec-F2F gate + its shared data-prep.

Every case here is a MEASURED failure mode from the book, not a hypothetical: the gate asserts
to a VP that an in-person executive meeting happened, and it got 2 of 6 "done" verdicts wrong
before adversarial review. Run with `python test_deal_engine_f2f.py` (stdlib only, no pytest
required) or under pytest — the test_* functions work either way.

Groups: exec-title traps, room mailboxes, marker boundaries, virtual veto, hybrid override,
aspiration, the shared prep (window / date precedence / Avoma '##' bodies / overwrite rank),
and the evidence contract (evidence must quote the text the marker was actually found in).
"""
from datetime import date

from deal_engine_f2f import (derive_exec_f2f, is_exec_title, is_real_person,
                             _has_in_person, _is_aspirational, _is_virtual)
from deal_engine_f2f_prep import (EVENT_ORDER_BY, F2F_WINDOW_DAYS, MAX_EVENTS, MAX_TASKS,
                                  TASK_ORDER_BY, clean_description, event_date,
                                  event_window_clause, should_replace, task_window_clause)

TODAY = date(2026, 1, 15)


def _ev(subject="", description="", location="", when="2025-12-01", attendees=None,
        description_raw=None):
    """description_raw defaults to `description` — matching the gate's own fallback for
    callers that predate the split (marker matching reads cleaned, the veto reads raw)."""
    return {"subject": subject, "description": description, "location": location,
            "date": when, "attendees": attendees or [],
            "description_raw": description if description_raw is None else description_raw}


CPO = {"name": "Ana Ruiz", "title": "Chief Procurement Officer", "email": "ana@buyer.com"}


# ── exec-title traps ────────────────────────────────────────────────────────────
def test_exec_titles():
    assert is_exec_title("Chief Procurement Officer") is True
    assert is_exec_title("Chief Financial Officer") is True
    assert is_exec_title("Managing Director") is True
    assert is_exec_title("Deputy Director") is True
    assert is_exec_title("VP, Procurement") is True
    assert is_exec_title("Vice President Supply Chain") is True
    assert is_exec_title("Head of Procurement") is True
    assert is_exec_title("Head Procurement") is True          # no "of" — real and senior
    assert is_exec_title("Global Head Supply Chain") is True
    assert is_exec_title("General Manager") is True
    assert is_exec_title("Founder") is True
    # "Director" outranks the junior word when both appear.
    assert is_exec_title("Director & Procurement Officer") is True
    # ...and the junior traps, which all contain "executive" or a chief-ish word.
    assert is_exec_title("Executive Assistant") is False
    assert is_exec_title("Assistant to the CEO") is False
    assert is_exec_title("Sales Executive") is False
    assert is_exec_title("Account Executive") is False
    assert is_exec_title("Tender Executive") is False
    assert is_exec_title("Procurement Executive") is False
    assert is_exec_title("Senior Specialist") is False        # Beko
    assert is_exec_title("Analyst") is False
    assert is_exec_title("Consultant") is False
    assert is_exec_title("Officer") is False                  # junior unless Chief
    assert is_exec_title("Team Lead") is False
    assert is_exec_title("Senior Manager") is False
    assert is_exec_title("NA") is False                       # 'NA' means UNKNOWN
    assert is_exec_title("") is False


# ── room mailboxes and address-shaped "Contacts" ────────────────────────────────
def test_room_mailboxes():
    assert is_real_person("John Smith", "john@acme.com") is True
    assert is_real_person("Boardroom 4A") is False            # Capitec
    assert is_real_person("Conference Room B", "confroom@acme.com") is False
    assert is_real_person("Jane Doe", "boardroom@acme.com") is False
    assert is_real_person("Auditorium") is False
    assert is_real_person("Zurich Office 3rd Floor") is False
    assert is_real_person("123 Main Street") is False
    assert is_real_person("") is False


# ── marker boundaries ───────────────────────────────────────────────────────────
def test_marker_boundaries():
    assert _has_in_person("Onsite workshop") == "onsite"
    assert _has_in_person("Face-to-Face review") == "face-to-face"
    assert _has_in_person("F2F with CPO") == "f2f"
    assert _has_in_person("Trade show booth staffing") == "booth"
    assert _has_in_person("Met at the conference in Berlin") == "conference"
    assert _has_in_person("website visit report") is None     # not "site visit"
    assert _has_in_person("revisit the pricing") is None
    assert _has_in_person("Conference Call with team") is None
    assert _has_in_person("Video Conference sync") is None
    assert _has_in_person("Quarterly business review") is None
    assert _has_in_person("") is None


# ── virtual veto + hybrid override ──────────────────────────────────────────────
def test_virtual_veto():
    assert _is_virtual("Join Zoom Meeting https://zoom.us/j/123", "") is True
    assert _is_virtual("", "Microsoft Teams Meeting") is True
    assert _is_virtual("Lunch at the Hilton", "") is False
    # Hybrid: an explicit physical assertion beats the join link (Tata).
    assert _is_virtual("teams.microsoft.com/l/x — all participants physically present "
                       "in one room", "") is False
    assert _is_virtual("Address: 5 Hill Rd. Dial-in if you must.", "") is False


def test_hybrid_override_still_scores_done():
    v = derive_exec_f2f(events=[_ev(
        subject="Business review",
        description="Teams link: teams.microsoft.com/l/x — all participants physically "
                    "present in one room at our Pune office.",
        attendees=[CPO])], today=TODAY)
    assert v["status"] == "done"
    assert v["exec_title"] == "Chief Procurement Officer"


# ── aspiration: a plan is not a record of something that happened ───────────────
def test_aspiration():
    assert _is_aspirational("Zycus team is available to visit the Chicago office") is True
    assert _is_aspirational("Onsite meeting scheduled for next week") is True
    assert _is_aspirational("Hosted us at their HQ last Tuesday") is False
    # Past tense in the same breath rescues a stray "invite".
    assert _is_aspirational("Invite: onsite. We met last week and it went very well") is False


def test_allstate_offer_is_planned_not_done():
    """An OFFER to visit, with an exec on the invite, must never read as a completed onsite."""
    v = derive_exec_f2f(events=[_ev(
        subject="Sync",
        description="Zycus team is available to visit the Chicago office for an onsite session.",
        attendees=[CPO])], today=TODAY)
    assert v["status"] == "planned"
    assert v["near_miss"] is False


def test_future_dated_onsite_is_planned():
    v = derive_exec_f2f(events=[_ev(subject="Onsite exec review", when="2026-03-02",
                                    attendees=[CPO])], today=TODAY)
    assert v["status"] == "planned"


def test_goldman_planned_task():
    v = derive_exec_f2f(tasks=[{"subject": "Onsite Meet Up", "status": "Planned",
                                "date": "2026-02-01"}], today=TODAY)
    assert v["status"] == "planned"
    assert v["near_miss"] is False


def test_next_step_only_is_planned_without_a_date():
    v = derive_exec_f2f(next_step="Plan to meet onsite in Q2", today=TODAY)
    assert v["status"] == "planned"
    assert v["date"] is None


def test_capitec_room_only_attendees_is_near_miss():
    v = derive_exec_f2f(events=[_ev(subject="In-person steering committee",
                                    attendees=[{"name": "Boardroom 4A", "title": "CEO"}])],
                        today=TODAY)
    assert v["status"] == "planned" and v["near_miss"] is True
    assert v["attendees_found"] == 0


def test_beko_all_junior_is_near_miss():
    v = derive_exec_f2f(events=[_ev(subject="Onsite factory walkthrough",
                                    attendees=[{"name": "Ola Kim",
                                                "title": "Senior Specialist"}])], today=TODAY)
    assert v["status"] == "planned" and v["near_miss"] is True
    assert v["attendees_found"] == 1


def test_virtual_invite_never_scores_done():
    v = derive_exec_f2f(events=[_ev(subject="Onsite kickoff",
                                    description="Join Zoom Meeting https://zoom.us/j/9",
                                    attendees=[CPO])], today=TODAY)
    assert v["status"] == "none"


def test_clean_done_verdict_shape():
    v = derive_exec_f2f(events=[_ev(subject="Onsite executive review",
                                    attendees=[CPO])], today=TODAY)
    assert v["status"] == "done"
    assert v["date"] == "2025-12-01"
    assert v["days_stale"] == 45
    assert v["exec_name"] == "Ana Ruiz"
    assert v["attendees_found"] == 1


def test_no_evidence_returns_none_not_empty_dict():
    """The sweep guard depends on this: a no-evidence run is truthy, so `if v:` is useless."""
    v = derive_exec_f2f(events=[], tasks=[], next_step="", today=TODAY)
    assert v["status"] == "none" and v["evidence"] is None
    assert bool(v) is True


# ── DEFECT 3: Avoma '##' note bodies describe the CUSTOMER, not the meeting ──────
ACTYLIS_DESC = (
    "Introductory Call with Actylis\n"
    "## Purpose\n"
    "Sage X3 ERP went live on site in February '22, replacing the legacy stack.\n"
    "## Notes\n"
    "AP team makes weekly on-site visits to review open invoices.\n")
# REAL Salesforce text, abridged — Event 00UP700000dx0ToMAI, "[Clari - Meeting] SMM Demo
# Session - Malmo", 2026-04-13, on the Bufab opp (006P700000Kpl1OIAR). The earlier fixture here
# was invented prose ("Elite Hotel Malmo, Storgatan 15") that exists nowhere in Salesforce, so it
# proved nothing about the real deal. Bufab's true markers are `conference` (this event's booked
# "Conference Room:") and `onsite` in the 2026-04-09 subject.
# This is also the sharpest test of the raw virtual veto: the invite carries a full Teams block
# — join link, "Meeting ID:", "Passcode:", dial-in — AND a street address, so only the
# _PHYSICAL_OVERRIDE keeps it a true positive. Get the override wrong and a genuine booked
# conference room with catering reads as a Teams call.
BUFAB_DESC = (
    "Hello Everyone,Please find the information for the demo session below:\r\n"
    "Hotel Information:Comfort Hotel Malmo\r\n"
    "Address: Carlsgatan 10 C, 211 20 Malmo, Sweden\r\n"
    "Booking Under the name of BUFAB.Check-In from 15:00 on 12thApril.\r\n"
    "Conference Room:\r\nLocation: Comfort Hotel Malmo\r\nBooked: 13-14 April\r\n"
    "Catering :\r\n13 & 14 April\r\nBreakfast\r\nMorning fika\r\nLunch\r\n"
    "Sessions:\r\nDay 1: 13thApril - IvaluaDay 2: 14thApril - Zycus\r\n"
    "________________________________________\r\nMicrosoft Teams meeting\r\n"
    "Join:\r\nhttps://teams.microsoft.com/meet/31526537878152?p=yr4AP8LaeewdILk8SU\r\n"
    "Meeting ID:\r\n315 265 378 781 52\r\nPasscode:\r\nTA9JB2ww\r\n"
    "Dial in by phone\r\n+46 8 505 218 97,,650165612#\r\nSweden, Stockholm\r\n")

# REAL subject — the SGD Pharma Event that flipped planned -> done. The "on Site" marker is in
# the SUBJECT, so no amount of body-cleaning can reach it; the Teams tell lived in the '##' body
# that cleaning strips. It is a PREPARATION CALL about an onsite, and the date it asserts
# (the call) is not the onsite date at all.
SGD_SUBJECT = "Avoma - : SGD - Preparation of our on Site WS (28th and 29th of October)"
SGD_DESC_RAW = (
    "-- Avoma Note Start --\n"
    "Avoma Meeting: https://app.avoma.com/meetings/1c0e9f22-4d31-4a7b-9e02-aa1/notes\n"
    "## Participants\n  * Zycus: A. Rep\n"
    "## Agenda\nJoin: https://teams.microsoft.com/meet/9912345?p=abc\n"
    "Meeting ID: 991 234 5\nPasscode: xY7\n"
    "## Key Takeaways\n  * Align on the agenda for the on site workshop.\n"
    "-- Avoma Note End --")


def test_clean_description_strips_avoma_body():
    assert clean_description(ACTYLIS_DESC).strip() == "Introductory Call with Actylis"
    assert "on site" not in clean_description(ACTYLIS_DESC)
    # A true positive has no '##' heading at all, so cleaning strips no prose — the booked
    # conference room, the address and the Teams block all survive (only the URL is truncated).
    cleaned_bufab = clean_description(BUFAB_DESC)
    assert "Conference Room:" in cleaned_bufab and "Address:" in cleaned_bufab
    assert "Microsoft Teams meeting" in cleaned_bufab
    assert clean_description(cleaned_bufab) == cleaned_bufab      # idempotent
    assert clean_description(clean_description(ACTYLIS_DESC)) == clean_description(ACTYLIS_DESC)
    # '##' mid-prose is not a heading; only line-anchored headings split the body.
    assert clean_description("we tagged it ## priority onsite") == "we tagged it ## priority onsite"
    assert clean_description(None) == "" and clean_description("") == ""


def test_urls_are_truncated_to_their_host():
    """A marker must never be read out of a URL path — but the virtual veto reads hostnames,
    so those must survive. International SOS was credited "done" off the "4f2f" inside an
    Avoma meeting UUID."""
    got = clean_description("Avoma Meeting: https://app.avoma.com/meetings/82f42066-450f-"
                            "4f2f-b03a-b4721aa17cb4/notes")
    assert got.endswith("https://app.avoma.com")
    assert "4f2f" not in got
    assert _has_in_person(got) is None
    # veto hostnames survive intact
    assert _is_virtual(clean_description("Join: https://teams.microsoft.com/meet/434?p=x"),
                       "") is True
    assert _is_virtual(clean_description("Join Zoom Meeting https://zoom.us/j/123"), "") is True


def test_actylis_drops_out_after_cleaning():
    """Raw, the customer's own ERP history credits a virtual call as a completed onsite."""
    raw = derive_exec_f2f(events=[_ev(subject="Introductory Call",
                                      description=ACTYLIS_DESC, attendees=[CPO])], today=TODAY)
    assert raw["status"] == "done"              # the false positive, reproduced
    cleaned = derive_exec_f2f(events=[_ev(subject="Introductory Call",
                                          description=clean_description(ACTYLIS_DESC),
                                          attendees=[CPO])], today=TODAY)
    assert cleaned["status"] == "none"          # ...and gone once the '##' body is stripped


def test_bufab_true_positive_survives_cleaning():
    """Bufab's real Malmo invite: a booked conference room WITH a Teams block. The street
    address overrides the join tell, so it stays done even once the veto reads the raw body."""
    v = derive_exec_f2f(events=[_ev(subject="Bufab executive session",
                                    description=clean_description(BUFAB_DESC),
                                    description_raw=BUFAB_DESC,
                                    attendees=[CPO])], today=date(2026, 5, 1))
    assert v["status"] == "done"
    assert v["evidence"]
    assert _has_in_person(clean_description(BUFAB_DESC)) == "conference"


# ── DEFECT B: the virtual veto must read the RAW body, marker-matching the cleaned one ──
def test_virtual_veto_reads_the_raw_description():
    """Cleaning strips the '##' body — including the join link inside it. The tell must
    still be seen, or stripping the body silently disables the veto."""
    cleaned = clean_description(SGD_DESC_RAW)
    assert "teams.microsoft.com" not in cleaned          # cleaning removed the tell...
    assert _is_virtual(cleaned, "") is False             # ...so the cleaned text is blind
    assert _is_virtual(cleaned, "", SGD_DESC_RAW) is True  # ...and the raw body still vetoes


def test_sgd_pharma_prep_call_is_not_a_completed_onsite():
    """The marker is in the SUBJECT, so cleaning cannot reach it; only the raw-body veto
    stops a PREPARATION CALL about an onsite from asserting the onsite happened."""
    ev = _ev(subject=SGD_SUBJECT, description=clean_description(SGD_DESC_RAW),
             description_raw=SGD_DESC_RAW, when="2025-10-20", attendees=[CPO])
    assert derive_exec_f2f(events=[ev], today=TODAY)["status"] != "done"
    # ...and the regression it replaced: without the raw body it flips straight back to done.
    blind = dict(ev); blind.pop("description_raw")
    assert derive_exec_f2f(events=[blind], today=TODAY)["status"] == "done"


def test_physical_override_is_not_read_from_the_stripped_body():
    """The override stays on the CLEANED text: "at our office" inside a '##' note describes
    the CUSTOMER'S premises (Actylis), and must never cancel a real join link."""
    raw = ("Weekly sync\nJoin: https://zoom.us/j/55\n"
           "## Background\n  * Their AP team sits at our office in Pune.\n")
    assert _is_virtual(clean_description(raw), "", raw) is True


# ── DEFECTS C + D: the two writers must select the SAME rows ────────────────────
def test_window_predicate_covers_all_day_events():
    """Sharing only the CONSTANT was not enough. All-day Events have a NULL ActivityDateTime,
    so an ActivityDateTime-only filter is blind to rows the other writer can see."""
    clause = event_window_clause()
    assert "ActivityDateTime >= LAST_N_DAYS:730" in clause
    assert "ActivityDate >= LAST_N_DAYS:730" in clause
    assert clause.startswith("(") and " OR " in clause


def test_window_predicate_qualifies_related_fields():
    assert event_window_clause("Event") == (
        "(Event.ActivityDateTime >= LAST_N_DAYS:730 OR Event.ActivityDate >= LAST_N_DAYS:730)")
    assert task_window_clause() == "ActivityDate >= LAST_N_DAYS:730"


def test_ordering_and_caps_are_shared():
    """19 opps exceed 200 events and 181 exceed 200 tasks, so a different ORDER BY or cap
    means the two writers read a different 200 and each overwrites the other."""
    assert MAX_EVENTS == 200 and MAX_TASKS == 400
    # Ordering must be on the date the verdict actually asserts (event_date prefers ActivityDate).
    assert EVENT_ORDER_BY == "ORDER BY ActivityDate DESC NULLS LAST"
    assert TASK_ORDER_BY == "ORDER BY ActivityDate DESC NULLS LAST"


# ── DEFECT 5: evidence must quote the text the marker was found in ──────────────
def test_evidence_from_description_when_subject_is_blank():
    v = derive_exec_f2f(events=[_ev(
        subject="",
        description="The team hosted us at their Frankfurt HQ for an onsite workshop.",
        attendees=[CPO])], today=TODAY)
    assert v["status"] == "done"
    assert v["evidence"]                                  # never empty on a done verdict
    assert "hosted us" in v["evidence"].lower()


def test_evidence_prefixes_subject_when_marker_is_in_the_body():
    v = derive_exec_f2f(events=[_ev(
        subject="Meeting",
        description="Agenda attached. The onsite executive session runs 10:00-14:00.",
        attendees=[CPO])], today=TODAY)
    assert v["evidence"].startswith("Meeting — ")
    assert "onsite" in v["evidence"].lower()              # not just the useless "Meeting"


def test_evidence_is_capped_at_200_chars():
    v = derive_exec_f2f(events=[_ev(
        subject="Meeting",
        description=("filler " * 60) + "onsite executive session" + (" filler" * 60),
        attendees=[CPO])], today=TODAY)
    assert 0 < len(v["evidence"]) <= 200


def test_evidence_unchanged_when_marker_is_in_the_subject():
    v = derive_exec_f2f(events=[_ev(subject="Onsite executive review",
                                    description="Agenda attached.", attendees=[CPO])],
                        today=TODAY)
    assert v["evidence"] == "Onsite executive review"


def test_evidence_never_empty_on_a_non_none_verdict():
    cases = [
        derive_exec_f2f(events=[_ev(subject="Onsite exec review", attendees=[CPO])], today=TODAY),
        derive_exec_f2f(events=[_ev(subject="", description="onsite walkthrough happened",
                                    attendees=[CPO])], today=TODAY),
        derive_exec_f2f(events=[_ev(subject="Onsite factory tour")], today=TODAY),
        derive_exec_f2f(tasks=[{"subject": "Onsite Meet Up", "status": "Planned",
                                "date": "2026-02-01"}], today=TODAY),
        derive_exec_f2f(next_step="Plan to meet onsite in Q2", today=TODAY),
    ]
    for v in cases:
        assert v["status"] in ("done", "planned")
        assert v["evidence"], f"empty evidence on {v['status']}: {v}"


# ── DEFECTS 1 + 2: the shared prep both writers must agree on ───────────────────
def test_shared_window_is_730():
    assert F2F_WINDOW_DAYS == 730


def test_event_date_prefers_the_local_calendar_day():
    # Orora / ARUP: ActivityDateTime is UTC and lands a day off the day a human would name.
    assert event_date("2025-11-25", "2025-11-26T01:30:00Z") == "2025-11-25"
    assert event_date("2026-01-09", "2026-01-10T02:00:00Z") == "2026-01-09"
    assert event_date(None, "2026-01-10T02:00:00Z") == "2026-01-10T02:00:00Z"
    assert event_date(None, None) is None


def test_should_replace_ranks_verdicts():
    done = {"status": "done"}
    planned = {"status": "planned"}
    none = {"status": "none"}
    assert should_replace(none, None) is True          # nothing stored yet
    assert should_replace(none, {}) is True
    assert should_replace(done, none) is True
    assert should_replace(planned, none) is True
    assert should_replace(done, planned) is True
    assert should_replace(done, done) is True          # equal rank refreshes staleness
    assert should_replace(none, done) is False         # the measured downgrade
    assert should_replace(none, planned) is False
    assert should_replace(planned, done) is False
    assert should_replace({}, done) is False
    assert should_replace(None, done) is False


if __name__ == "__main__":
    import sys, traceback
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} tests passed")
    sys.exit(1 if failed else 0)

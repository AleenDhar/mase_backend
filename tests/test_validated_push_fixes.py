"""
Tests for lemlist_validated_push Fix #1 (docstring) + Fix #3 (fail-closed
on empty custom_fields_per_email).

Run: python3 tests/test_validated_push_fixes.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEMLIST_API_KEY", "test_key_not_used")

import lemlist_mcp_server as L  # noqa: E402

# @mcp.tool() wraps the function in a FastMCP FunctionTool — call .fn for the
# underlying Python callable in tests.
_validated_push = L.lemlist_validated_push.fn


def _install_mocks(posted, receipts):
    """Replace every external dependency so validated_push runs offline."""
    L._gateway_sf_lookup_contacts = lambda ids: {
        cid: {
            "Id": cid,
            "AccountId": "001ACME0000001AAA",
            "Email": f"contact{i}@example.com",
            "FirstName": f"First{i}",
            "LastName": f"Last{i}",
            "Title": "VP Procurement",
            "MobilePhone": "+10000000000",
            "Account": {"Name": "ACME Inc"},
        }
        for i, cid in enumerate(ids)
    }
    L._gateway_resolve_owner = lambda email: {
        "_id": "usr_FAKE_OWNER",
        "email": email,
        "campaigns": [{"_id": "cam_TEST"}],
    }
    L._get = lambda path: ({"_id": "cam_TEST"} if "/campaigns/" in path else {})
    L._gateway_pre_flight = lambda email, cid: {"state": "available"}
    L._post = lambda path, payload: (
        posted.append((path, payload)),
        {"_id": f"lea_FAKE_{len(posted):03d}"},
    )[1]
    L._gateway_write_receipt = lambda row: (
        receipts.append(row),
        f"rec_{len(receipts):03d}",
    )[1]
    L._sync_campaign_date_to_salesforce = lambda *a, **kw: None


def run(label, fn):
    try:
        fn()
        print(f"  PASS  {label}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {label}\n        {e}")
        return False
    except Exception as e:
        print(f"  ERR   {label}\n        {type(e).__name__}: {e}")
        return False


def test_build_payload_forwards_all_custom_vars():
    sf_row = {
        "Id": "003X", "AccountId": "001A",
        "Email": "alice@co.com", "FirstName": "Alice", "LastName": "Smith",
        "Title": "VP", "MobilePhone": "+1", "Account": {"Name": "Co"},
    }
    custom = {
        "customSubject1": "S1", "customBody1": "B1",
        "customBridge1": "Br1", "customValue1": "V1",
        "CTA1": "CTA", "linkedInMessage": "LI",
        "email": "SPOOF@evil.com", "firstName": "SPOOF",  # identity spoof
    }
    p = L._gateway_build_payload(sf_row, "usr_X", custom)
    assert p["customSubject1"] == "S1"
    assert p["customBody1"] == "B1"
    assert p["customBridge1"] == "Br1"
    assert p["customValue1"] == "V1"
    assert p["CTA1"] == "CTA"
    assert p["linkedInMessage"] == "LI"
    assert p["email"] == "alice@co.com", "identity spoof must be stripped"
    assert p["firstName"] == "Alice", "identity spoof must be stripped"


def test_fix3_rejects_when_custom_fields_per_email_is_none():
    posted, receipts = [], []
    _install_mocks(posted, receipts)
    r = json.loads(_validated_push(
        chat_id="t1", account_id="001ACME0000001AAA",
        campaign_id="cam_TEST", owner_email="bd@zycus.com",
        contact_sf_ids=["003C00000000001"],
    ))
    assert len(posted) == 0, "should not POST when no custom vars"
    assert r["summary"]["rejected"] == 1
    assert r["rejected"][0]["reason"] == "no_custom_vars"
    assert receipts[0]["action"] == "rejected_no_custom_vars"
    assert "allow_empty_custom_fields=True" in receipts[0]["error"]


def test_fix3_rejects_when_email_key_missing():
    posted, receipts = [], []
    _install_mocks(posted, receipts)
    r = json.loads(_validated_push(
        chat_id="t2", account_id="001ACME0000001AAA",
        campaign_id="cam_TEST", owner_email="bd@zycus.com",
        contact_sf_ids=["003C00000000001"],
        custom_fields_per_email={"someone_else@co.com": {"customSubject1": "S"}},
    ))
    assert len(posted) == 0
    assert r["rejected"][0]["reason"] == "no_custom_vars"


def test_fix3_rejects_when_dict_empty_for_email():
    posted, receipts = [], []
    _install_mocks(posted, receipts)
    r = json.loads(_validated_push(
        chat_id="t3", account_id="001ACME0000001AAA",
        campaign_id="cam_TEST", owner_email="bd@zycus.com",
        contact_sf_ids=["003C00000000001"],
        custom_fields_per_email={"contact0@example.com": {}},
    ))
    assert len(posted) == 0
    assert r["rejected"][0]["reason"] == "no_custom_vars"


def test_fix3_passes_when_custom_vars_present():
    posted, receipts = [], []
    _install_mocks(posted, receipts)
    r = json.loads(_validated_push(
        chat_id="t4", account_id="001ACME0000001AAA",
        campaign_id="cam_TEST", owner_email="bd@zycus.com",
        contact_sf_ids=["003C00000000001"],
        custom_fields_per_email={
            "contact0@example.com": {
                "customSubject1": "Subject for Alice",
                "customBody1": "Body for Alice",
                "CTA1": "Book a call",
                "linkedInMessage": "Hi Alice",
            },
        },
    ))
    assert len(posted) == 1, "should POST exactly once"
    _, payload = posted[0]
    assert payload["customSubject1"] == "Subject for Alice"
    assert payload["customBody1"] == "Body for Alice"
    assert payload["CTA1"] == "Book a call"
    assert payload["linkedInMessage"] == "Hi Alice"
    assert payload["email"] == "contact0@example.com"
    assert payload["contactOwner"] == "usr_FAKE_OWNER"
    assert r["summary"]["pushed"] == 1
    assert r["summary"]["rejected"] == 0


def test_fix3_bypass_with_allow_empty_flag():
    posted, receipts = [], []
    _install_mocks(posted, receipts)
    r = json.loads(_validated_push(
        chat_id="t5", account_id="001ACME0000001AAA",
        campaign_id="cam_TEST", owner_email="bd@zycus.com",
        contact_sf_ids=["003C00000000001"],
        allow_empty_custom_fields=True,
    ))
    assert len(posted) == 1, "should POST when bypass flag is set"
    _, payload = posted[0]
    assert not any(k.startswith("custom") for k in payload), \
        "no custom keys expected when bypass flag is used"
    assert r["summary"]["pushed"] == 1


def test_fix3_mixed_batch_partial_reject():
    posted, receipts = [], []
    _install_mocks(posted, receipts)
    r = json.loads(_validated_push(
        chat_id="t6", account_id="001ACME0000001AAA",
        campaign_id="cam_TEST", owner_email="bd@zycus.com",
        contact_sf_ids=["003C00000000001", "003C00000000002", "003C00000000003"],
        custom_fields_per_email={
            "contact0@example.com": {"customSubject1": "A"},
            # contact1: missing entirely
            "contact2@example.com": {"customSubject1": "C"},
        },
    ))
    assert len(posted) == 2, f"expected 2 pushes, got {len(posted)}"
    assert r["summary"]["pushed"] == 2
    assert r["summary"]["rejected"] == 1
    assert any(rj["reason"] == "no_custom_vars" and rj["email"] == "contact1@example.com"
               for rj in r["rejected"])


def test_fix1_docstring_critical_info_in_first_400_chars():
    """The truncation cap is 400 chars — usage hint must fit inside it."""
    import ast
    src = open("lemlist_mcp_server.py").read()
    tree = ast.parse(src)
    ds = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "lemlist_validated_push"):
            ds = ast.get_docstring(node) or ""
            break
    assert ds is not None, "docstring not found"
    head = ds[:400]
    assert "custom_fields_per_email" in head, \
        "param name missing from first 400 chars (truncated by TOOL_DESCRIPTION_MAX_CHARS)"
    assert "customSubject1" in head, \
        "concrete var example missing from first 400 chars"
    assert "email" in head.lower(), \
        "email-keyed format hint missing from first 400 chars"


TESTS = [
    ("payload forwards all custom vars + strips identity spoof",
     test_build_payload_forwards_all_custom_vars),
    ("Fix #3: rejects when custom_fields_per_email is None",
     test_fix3_rejects_when_custom_fields_per_email_is_none),
    ("Fix #3: rejects when email key missing from dict",
     test_fix3_rejects_when_email_key_missing),
    ("Fix #3: rejects when email's dict is empty",
     test_fix3_rejects_when_dict_empty_for_email),
    ("Fix #3: passes through when custom vars present",
     test_fix3_passes_when_custom_vars_present),
    ("Fix #3: allow_empty_custom_fields=True bypasses check",
     test_fix3_bypass_with_allow_empty_flag),
    ("Fix #3: mixed batch — partial reject, partial push",
     test_fix3_mixed_batch_partial_reject),
    ("Fix #1: docstring usage hint inside 400-char truncation cap",
     test_fix1_docstring_critical_info_in_first_400_chars),
]

if __name__ == "__main__":
    print("=" * 72)
    print("lemlist_validated_push Fix #1 + #3 test suite")
    print("=" * 72)
    results = [run(label, fn) for label, fn in TESTS]
    print("=" * 72)
    print(f"  {sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)

import requests
import json
import time

BASE_URL = "http://localhost:5000"


def test_endpoint(name, user_message):
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    try:
        payload = {
            "messages": [{"role": "user", "content": user_message}],
            "stream": True,
            "model": "openai:gpt-4o"
        }
        r = requests.post(
            f"{BASE_URL}/api/chat",
            json=payload,
            stream=True,
            timeout=180
        )
        if r.status_code != 200:
            print(f"  HTTP {r.status_code}: {r.text[:300]}")
            return {"name": name, "status": "FAIL", "tools": [], "errors": [f"HTTP {r.status_code}"]}

        result_text = ""
        tool_calls = []
        errors = []

        for line in r.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8")
            if decoded.startswith("data: "):
                data_str = decoded[6:]
                if data_str == "[DONE]":
                    break
                try:
                    evt = json.loads(data_str)
                    t = evt.get("type", "")
                    if t == "tool_call":
                        tool_name = evt.get("name", "unknown")
                        tool_calls.append(tool_name)
                        print(f"  [TOOL CALL] {tool_name}")
                    elif t == "tool_result":
                        content = evt.get("content", "")
                        snippet = content[:300] if isinstance(content, str) else str(content)[:300]
                        is_error = '"error"' in snippet.lower()
                        status = "ERROR" if is_error else "OK"
                        print(f"  [TOOL RESULT] {status}: {snippet[:150]}...")
                        if is_error:
                            errors.append(snippet[:300])
                    elif t == "token":
                        result_text += evt.get("content", "")
                    elif t == "error":
                        errors.append(evt.get("content", "unknown error"))
                        print(f"  [ERROR] {evt.get('content', '')[:150]}")
                except json.JSONDecodeError:
                    pass

        status = "PASS" if not errors else "FAIL"
        print(f"\n  Status: {status}")
        print(f"  Tools called: {tool_calls}")
        if errors:
            for e in errors:
                print(f"  Error: {e[:200]}")
        if result_text:
            print(f"  Response preview: {result_text[:400]}")
        return {"name": name, "status": status, "tools": tool_calls, "errors": errors}

    except Exception as e:
        print(f"  EXCEPTION: {e}")
        return {"name": name, "status": "FAIL", "tools": [], "errors": [str(e)]}


tests = [
    ("ZoomInfo - Search Companies",
     "Use the zi_search_companies tool to search for Microsoft by companyDomain=['microsoft.com'], resultsPerPage=3. Only call this one tool and report what you get back."),

    ("ZoomInfo - Enrich Company",
     "Use the zi_enrich_company tool with companyDomain='microsoft.com'. Only call this one tool and report what you get back."),

    ("ZoomInfo - Get Technologies",
     "Use the zi_get_technologies tool with companyDomain='microsoft.com'. Only call this one tool and report what you get back."),

    ("Seamless.ai - Search Contacts",
     "Use the seamless_search_contacts tool with companyDomain=['microsoft.com'], jobTitle=['CTO'], limit=3. Only call this one tool and report what you get back."),

    ("Seamless.ai - Search Companies",
     "Use the seamless_search_companies tool with companyDomain=['microsoft.com'], limit=3. Only call this one tool and report what you get back."),

    ("Apollo - Search People",
     "Use the apollo_search_people tool to find CTOs at Microsoft, limit to 3 results. Only call this one tool and report what you get back."),
]


if __name__ == "__main__":
    print("=" * 60)
    print("MCP ENDPOINT TEST SUITE")
    print(f"Server: {BASE_URL}")
    print(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Tests: {len(tests)}")
    print("=" * 60)

    results = []
    for name, msg in tests:
        result = test_endpoint(name, msg)
        results.append(result)
        time.sleep(2)

    print("\n\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print(f"Total: {len(results)} | Passed: {passed} | Failed: {failed}\n")
    for r in results:
        icon = "PASS" if r["status"] == "PASS" else "FAIL"
        tools = ", ".join(r["tools"]) if r["tools"] else "none"
        print(f"  [{icon}] {r['name']}")
        print(f"         Tools: {tools}")
        if r["errors"]:
            for e in r["errors"]:
                print(f"         Error: {e[:200]}")
    print()

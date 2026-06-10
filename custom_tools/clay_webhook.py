import json
import os
from typing import Any, Dict, List

import httpx
from langchain_core.tools import tool


CLAY_WEBHOOK_URL = os.environ.get("CLAY_WEBHOOK_URL", "")
TIMEOUT = 30.0


def _transform_contact(contact: Any) -> Dict[str, Any]:
    if not isinstance(contact, dict):
        return {}

    first = str(contact.get("first_name") or "").strip()
    last = str(contact.get("last_name") or "").strip()
    full_name = f"{first} {last}".strip()

    row: Dict[str, Any] = {}
    if first:
        row["First Name"] = first
    if last:
        row["Last Name"] = last
    if full_name:
        row["name"] = full_name

    email = str(contact.get("email") or "").strip()
    if email:
        row["Email"] = email

    title = str(contact.get("title") or "").strip()
    if title:
        row["title"] = title

    linkedin = str(contact.get("linkedin_url") or "").strip()
    if linkedin:
        row["LinkedIn Profile URL"] = linkedin

    phone = str(contact.get("phone") or "").strip()
    if phone:
        row["Mobile Phone"] = phone

    return row


@tool
def send_to_clay(contacts: List[Dict[str, Any]]) -> str:
    """Send a list of contacts to the Clay table via webhook.

    Use this tool to push contact data into Clay. Each contact should have
    the following fields (all optional but at least one required):
      - first_name: Contact's first name
      - last_name: Contact's last name
      - email: Contact's email address
      - title: Job title (e.g. "VP Sales")
      - linkedin_url: Full LinkedIn profile URL
      - phone: Mobile phone number

    The tool automatically maps these fields to Clay's column schema:
      first_name  → "First Name"
      last_name   → "Last Name"
      (combined)  → "name"
      email       → "Email"
      title       → "title"
      linkedin_url → "LinkedIn Profile URL"
      phone       → "Mobile Phone"

    Args:
        contacts: List of contact dictionaries.
                  Example: [{"first_name": "John", "last_name": "Doe",
                             "email": "john@acme.com", "title": "VP Sales",
                             "linkedin_url": "https://linkedin.com/in/johndoe",
                             "phone": "+1 971 442 6036"}]

    Returns:
        JSON string with status and number of records sent.
    """
    if not CLAY_WEBHOOK_URL:
        return json.dumps({"error": "CLAY_WEBHOOK_URL environment variable is not set."})

    if not contacts:
        return json.dumps({"error": "No contacts provided. Pass a non-empty list of contact dictionaries."})

    transformed = [_transform_contact(c) for c in contacts]
    payload = [row for row in transformed if row]
    skipped = len(contacts) - len(payload)

    if not payload:
        return json.dumps({"error": "All contacts were empty after transformation. Provide at least one field per contact."})

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                CLAY_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()

            try:
                body = response.json()
            except Exception:
                body = response.text

            print(f"[CLAY] Successfully sent {len(payload)} contacts. HTTP {response.status_code}")

            result = {
                "status": "success",
                "records_sent": len(payload),
                "http_status": response.status_code,
                "response": body,
            }
            if skipped:
                result["records_skipped"] = skipped
            return json.dumps(result, indent=2, default=str)

    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        print(f"[CLAY] HTTP error {e.response.status_code} sending {len(payload)} contacts")
        return json.dumps({
            "error": f"HTTP {e.response.status_code} error from Clay webhook",
            "detail": detail,
        }, indent=2)
    except httpx.TimeoutException:
        print(f"[CLAY] Timeout sending {len(payload)} contacts")
        return json.dumps({"error": "Request to Clay webhook timed out. Please retry."})
    except Exception as e:
        print(f"[CLAY] Unexpected error: {type(e).__name__}: {e}")
        return json.dumps({"error": f"Unexpected error: {type(e).__name__}: {e}"})

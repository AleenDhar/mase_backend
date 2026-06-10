import re
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

UNICODE_REPLACEMENTS = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201C": '"',
    "\u201D": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2026": "...",
    "\u00A0": " ",
    "\u200B": "",
    "\u200C": "",
    "\u200D": "",
    "\uFEFF": "",
    "\u00AD": "",
    "\u2022": "-",
    "\u2033": '"',
    "\u2032": "'",
    "\u00B7": "-",
}

PROTECTED_FIELDS = {"email", "firstName", "lastName", "companyName"}


def clean_string(value: str) -> str:
    if not isinstance(value, str):
        return value

    for char, replacement in UNICODE_REPLACEMENTS.items():
        value = value.replace(char, replacement)

    value = re.sub(r'<br\s*/?\s*>', '\n', value, flags=re.IGNORECASE)
    value = re.sub(r'</(p|div|li|tr|h[1-6])>', '\n', value, flags=re.IGNORECASE)
    value = re.sub(r'<[^>]+>', '', value)
    value = re.sub(r'&amp;', '&', value)
    value = re.sub(r'&lt;', '<', value)
    value = re.sub(r'&gt;', '>', value)
    value = re.sub(r'&nbsp;', ' ', value)
    value = re.sub(r'&quot;', '"', value)
    value = re.sub(r'&#39;', "'", value)

    value = re.sub(r'[^\x20-\x7E\n]', '', value)
    value = re.sub(r' {2,}', ' ', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    value = re.sub(r'^(Re|Fwd|FW|RE)\s*:\s*', '', value, flags=re.IGNORECASE)

    return value.strip()


def sanitize_lead_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = {}

    for key, value in payload.items():
        if value is None:
            if key in PROTECTED_FIELDS:
                raise ValueError(f"Protected field '{key}' is None for lead: {payload.get('email', 'UNKNOWN')}")
            continue

        if isinstance(value, str):
            scrubbed = clean_string(value)

            if not scrubbed:
                if key in PROTECTED_FIELDS:
                    raise ValueError(f"Protected field '{key}' is empty after cleaning for lead: {payload.get('email', 'UNKNOWN')}")
                continue

            cleaned[key] = scrubbed

        elif isinstance(value, dict):
            nested = sanitize_lead_payload(value)
            if nested:
                cleaned[key] = nested

        elif isinstance(value, list):
            sanitized_list = [
                clean_string(item) if isinstance(item, str) else item
                for item in value
                if item is not None and item != ""
            ]
            if sanitized_list:
                cleaned[key] = sanitized_list

        else:
            cleaned[key] = value

    return cleaned


def sanitize_batch(payloads: list[dict]) -> tuple[list[dict], list[dict]]:
    valid = []
    rejected = []

    for payload in payloads:
        try:
            cleaned = sanitize_lead_payload(payload)
            valid.append(cleaned)
        except ValueError as e:
            rejected.append({
                "payload": payload,
                "error": str(e)
            })
            logger.error(f"Rejected lead: {e}")

    logger.info(f"Sanitization complete: {len(valid)} valid, {len(rejected)} rejected")
    return valid, rejected


def sanitize_tool_args(kwargs: dict) -> dict:
    sanitized = {}
    for key, value in kwargs.items():
        if isinstance(value, str):
            sanitized[key] = clean_string(value)
        elif isinstance(value, dict):
            try:
                sanitized[key] = sanitize_lead_payload(value)
            except ValueError:
                sanitized[key] = value
        elif isinstance(value, list):
            sanitized_list = []
            for item in value:
                if isinstance(item, dict):
                    try:
                        sanitized_list.append(sanitize_lead_payload(item))
                    except ValueError:
                        sanitized_list.append(item)
                elif isinstance(item, str):
                    sanitized_list.append(clean_string(item))
                else:
                    sanitized_list.append(item)
            sanitized[key] = sanitized_list
        else:
            sanitized[key] = value
    return sanitized

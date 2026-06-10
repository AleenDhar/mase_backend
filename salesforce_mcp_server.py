import os
import functools
from dotenv import load_dotenv
from fastmcp import FastMCP, Context
from simple_salesforce import Salesforce, SalesforceMalformedRequest

load_dotenv()

@functools.lru_cache(maxsize=1)
def sf_conn() -> Salesforce:
    """Establish and cache a Salesforce connection."""
    username = os.environ["SF_USERNAME"]
    password = os.environ["SF_PASSWORD"]
    security_token = os.environ["SF_SECURITY_TOKEN"]
    domain = os.environ.get("SF_DOMAIN", "login")
    return Salesforce(
        username=username,
        password=password,
        security_token=security_token,
        domain=domain
    )

mcp = FastMCP("salesforce-mcp-server")

@mcp.tool
def soql(query: str, ctx: Context):
    """Run a SOQL query and return a list of records."""
    if not query or not query.strip():
        return {"error": "Query cannot be empty"}
    try:
        result = sf_conn().query_all(query)
        return result.get("records", [])
    except SalesforceMalformedRequest as e:
        return {"error": f"SOQL query failed: {e.content}"}
    except Exception as e:
        return {"error": f"SOQL query failed: {str(e)}"}

@mcp.tool
def get_record(object_api_name: str, record_id: str, ctx: Context):
    """Retrieve a single Salesforce record by ID."""
    if not object_api_name:
        return {"error": "Object API name is required"}
    if not record_id:
        return {"error": "Record ID is required"}
    try:
        obj = getattr(sf_conn(), object_api_name)
        record = obj.get(record_id)
        return record
    except SalesforceMalformedRequest as e:
        return {"error": f"Get record failed: {e.content}"}
    except Exception as e:
        return {"error": f"Failed to retrieve record: {str(e)}"}

@mcp.tool
def describe_object(object_api_name: str, ctx: Context):
    """Get metadata about a Salesforce object including fields, types, and relationships."""
    if not object_api_name:
        return {"error": "Object API name is required"}
    try:
        obj = getattr(sf_conn(), object_api_name)
        desc = obj.describe()
        return {
            "name": desc.get("name"),
            "label": desc.get("label"),
            "fields": desc.get("fields", []),
            "queryable": desc.get("queryable"),
            "createable": desc.get("createable"),
            "updateable": desc.get("updateable"),
            "deletable": desc.get("deletable")
        }
    except SalesforceMalformedRequest as e:
        return {"error": f"Describe failed: {e.content}"}
    except Exception as e:
        return {"error": f"Failed to describe object: {str(e)}"}

@mcp.tool
def list_objects(ctx: Context):
    """List all available Salesforce objects in the org."""
    try:
        desc = sf_conn().describe()
        objects = [{"name": obj["name"], "label": obj["label"]} for obj in desc.get("sobjects", [])]
        return {"objects": objects, "count": len(objects)}
    except Exception as e:
        return {"error": f"Failed to list objects: {str(e)}"}

@mcp.tool
def create_contact(
    last_name: str,
    first_name: str,
    email: str,
    account_id: str,
    title: str,
    phone: str = None,
    mobile_phone: str = None,
    department: str = None,
    mailing_street: str = None,
    mailing_city: str = None,
    mailing_state: str = None,
    mailing_postal_code: str = None,
    mailing_country: str = None,
    linkedin_url: str = None,
    description: str = None,
    lead_source: str = None,
    owner_id: str = None,
    extra_fields: dict = None,
    ctx: Context = None,
):
    """
    Create a new Contact record in Salesforce.

    Args:
        last_name: Contact's last name (required).
        first_name: Contact's first name (required).
        email: Email address (required).
        account_id: Salesforce Account ID to link this contact to (required).
        title: Job title (required).
        phone: Phone number.
        mobile_phone: Mobile phone number.
        department: Department name.
        mailing_street: Street address.
        mailing_city: City.
        mailing_state: State or province.
        mailing_postal_code: Postal/ZIP code.
        mailing_country: Country.
        linkedin_url: LinkedIn profile URL (stored in a custom field if available).
        description: Free-text description or notes.
        lead_source: How the contact was acquired (e.g. "Web", "Referral").
        owner_id: Salesforce User ID of the contact owner.
        extra_fields: Dict of any additional standard or custom field API names and values
            (e.g. {"Lemlist_Campaign_Added_Date__c": "2026-01-01T00:00:00.000+0000"}).

    Returns:
        Dict with the created Contact's id and success status, or an error message.
    """
    missing = []
    if not last_name or not last_name.strip():
        missing.append("last_name")
    if not first_name or not first_name.strip():
        missing.append("first_name")
    if not email or not email.strip():
        missing.append("email")
    if not account_id or not account_id.strip():
        missing.append("account_id")
    if not title or not title.strip():
        missing.append("title")
    if missing:
        return {"error": f"Missing required fields: {', '.join(missing)}"}

    # Validate AccountId format. Salesforce standard Account IDs always start
    # with the prefix `001`. A common failure mode is the user pasting an ID
    # where capital `O` was substituted for `0` (e.g. `00OP7000005SznF` instead
    # of `001P7000005SznF`). The SOQL endpoint silently accepts the typo'd ID
    # (returns no rows), but Contact.create rejects it with the cryptic
    # `Account ID: id value of incorrect type` FIELD_INTEGRITY_EXCEPTION
    # (chat b720c200, seqs 263-268). Catch it here with an actionable message.
    aid = account_id.strip()
    if not aid.startswith("001"):
        suggestion = ""
        if len(aid) >= 3 and aid[:3].upper() == "00O":
            # Common typo: capital O substituted for 0 in the prefix.
            suggestion = (
                f" Did you mean '001{aid[3:]}'? "
                "Capital `O` looks like `0` — Salesforce Account IDs always "
                "start with the digits `001`."
            )
        return {
            "error": (
                f"Invalid Account ID prefix: '{aid[:3]}'. Salesforce Account IDs "
                f"must start with '001' (got '{aid}').{suggestion}"
            )
        }

    data = {"LastName": last_name, "FirstName": first_name, "Email": email, "AccountId": aid, "Title": title}
    field_map = {
        "phone": "Phone",
        "mobile_phone": "MobilePhone",
        "department": "Department",
        "mailing_street": "MailingStreet",
        "mailing_city": "MailingCity",
        "mailing_state": "MailingState",
        "mailing_postal_code": "MailingPostalCode",
        "mailing_country": "MailingCountry",
        "description": "Description",
        "lead_source": "LeadSource",
        "owner_id": "OwnerId",
    }

    local_vars = {
        "phone": phone,
        "mobile_phone": mobile_phone,
        "department": department,
        "mailing_street": mailing_street,
        "mailing_city": mailing_city,
        "mailing_state": mailing_state,
        "mailing_postal_code": mailing_postal_code,
        "mailing_country": mailing_country,
        "description": description,
        "lead_source": lead_source,
        "owner_id": owner_id,
    }

    for param_name, sf_field in field_map.items():
        value = local_vars.get(param_name)
        if value is not None and (not isinstance(value, str) or value.strip()):
            data[sf_field] = value

    if linkedin_url:
        # Custom field on Contact: `LinkedIn_Profile__c` (matches the field used
        # in every successful SOQL on this org — e.g.
        # `SELECT ... LinkedIn_Profile__c ... FROM Contact`).
        # Previously this wrote to `LinkedIn_Profile_URL__c`, which does not
        # exist on the org and caused every create_contact call to fail with
        # INVALID_FIELD (chat b720c200, seqs 246-251).
        data["LinkedIn_Profile__c"] = linkedin_url

    if extra_fields and isinstance(extra_fields, dict):
        for k, v in extra_fields.items():
            if v is not None:
                data[k] = v

    try:
        result = sf_conn().Contact.create(data)
        return {"id": result.get("id"), "success": result.get("success", True), "fields_set": list(data.keys())}
    except SalesforceMalformedRequest as e:
        return {"error": f"Create contact failed: {e.content}"}
    except Exception as e:
        return {"error": f"Create contact failed: {str(e)}"}


@mcp.tool
def update_contact_email_from_ai(contact_id: str, email_from_ai: str, ctx: Context = None):
    """
    Update the Email_from_AI__c field on a Salesforce Contact.

    Use this tool when the AI determines that the existing email on a Contact
    record is incorrect and wants to store the corrected email address.
    This only updates the Email_from_AI__c custom field — no other fields are modified.

    Args:
        contact_id: The Salesforce Contact record ID (e.g. "003XXXXXXXXXXXX").
        email_from_ai: The corrected email address to store in Email_from_AI__c.

    Returns:
        Dict confirming the update with contact_id and the email set, or an error message.
    """
    if not contact_id or not contact_id.strip():
        return {"error": "contact_id is required"}
    if not email_from_ai or not email_from_ai.strip():
        return {"error": "email_from_ai is required"}
    try:
        sf_conn().Contact.update(contact_id, {"Email_from_AI__c": email_from_ai.strip()})
        return {"success": True, "contact_id": contact_id, "Email_from_AI__c": email_from_ai.strip()}
    except SalesforceMalformedRequest as e:
        return {"error": f"Update failed: {e.content}"}
    except Exception as e:
        return {"error": f"Update failed: {str(e)}"}


@mcp.tool
def set_hot_abm_status(account_id: str, status: str, ctx: Context = None):
    """
    Set the Hot_ABM_Status__c picklist field on a Salesforce Account.

    Use this to mark an Account's ABM status after running or completing
    an ABM outreach workflow. Valid picklist values must exactly match what is
    configured in Salesforce.

    Args:
        account_id: The Salesforce Account record ID (15 or 18 characters).
        status: The picklist value to set for Hot_ABM_Status__c.
                Must exactly match a configured picklist value in Salesforce.

    Returns:
        Dict confirming success with account_id and status set, or an error.
    """
    if not account_id or len(account_id.strip()) < 15:
        return {"error": "account_id must be a valid 15 or 18 character Salesforce Account ID"}
    if not status or not status.strip():
        return {"error": "status is required — provide a valid Hot_ABM_Status__c picklist value"}
    try:
        sf_conn().Account.update(account_id.strip(), {"Hot_ABM_Status__c": status.strip()})
        return {
            "success": True,
            "account_id": account_id.strip(),
            "Hot_ABM_Status__c": status.strip(),
        }
    except SalesforceMalformedRequest as e:
        return {"error": f"Update failed: {e.content}"}
    except Exception as e:
        return {"error": f"Update failed: {str(e)}"}


@mcp.tool
def create_record(object_api_name: str, fields: dict, ctx: Context = None):
    """Create any Salesforce record. Pass the sObject API name (e.g. 'Task', 'Account', 'Opportunity', 'Event') and a dict of field API names → values. Returns {id, success, errors}."""
    if not object_api_name:
        return {"error": "object_api_name is required"}
    if not isinstance(fields, dict) or not fields:
        return {"error": "fields must be a non-empty dict of fieldApiName -> value"}
    try:
        obj = getattr(sf_conn(), object_api_name)
        result = obj.create(fields)
        return result
    except SalesforceMalformedRequest as e:
        return {"error": f"Create failed: {e.content}"}
    except Exception as e:
        return {"error": f"Create failed: {str(e)}"}


@mcp.tool
def update_record(object_api_name: str, record_id: str, fields: dict, ctx: Context = None):
    """Update any Salesforce record by ID. Pass sObject API name, the record Id, and a dict of fields to change. Returns HTTP status (204 = success)."""
    if not object_api_name:
        return {"error": "object_api_name is required"}
    if not record_id:
        return {"error": "record_id is required"}
    if not isinstance(fields, dict) or not fields:
        return {"error": "fields must be a non-empty dict"}
    try:
        obj = getattr(sf_conn(), object_api_name)
        status = obj.update(record_id, fields)
        return {"status": status, "id": record_id, "success": status == 204}
    except SalesforceMalformedRequest as e:
        return {"error": f"Update failed: {e.content}"}
    except Exception as e:
        return {"error": f"Update failed: {str(e)}"}


@mcp.tool
def delete_record(object_api_name: str, record_id: str, ctx: Context = None):
    """Delete any Salesforce record by ID. Irreversible. Returns HTTP status (204 = success)."""
    if not object_api_name:
        return {"error": "object_api_name is required"}
    if not record_id:
        return {"error": "record_id is required"}
    try:
        obj = getattr(sf_conn(), object_api_name)
        status = obj.delete(record_id)
        return {"status": status, "id": record_id, "success": status == 204}
    except SalesforceMalformedRequest as e:
        return {"error": f"Delete failed: {e.content}"}
    except Exception as e:
        return {"error": f"Delete failed: {str(e)}"}


@mcp.tool
def create_task(
    subject: str,
    what_id: str = None,
    who_id: str = None,
    owner_id: str = None,
    activity_date: str = None,
    priority: str = "Normal",
    status: str = "Not Started",
    task_type: str = None,
    description: str = None,
    extra_fields: dict = None,
    ctx: Context = None,
):
    """Create a Salesforce Task. subject is required. what_id = related Account/Opportunity/Case Id; who_id = related Contact/Lead Id; owner_id = user assigned; activity_date = YYYY-MM-DD due date; priority = High/Normal/Low; status = Not Started/In Progress/Completed/Deferred; task_type = Call/Email/Meeting/etc.; description = body. Use extra_fields for any custom fields. Returns {id, success, errors}."""
    if not subject or not subject.strip():
        return {"error": "subject is required"}
    payload: dict = {
        "Subject": subject,
        "Priority": priority,
        "Status": status,
    }
    if what_id:
        payload["WhatId"] = what_id
    if who_id:
        payload["WhoId"] = who_id
    if owner_id:
        payload["OwnerId"] = owner_id
    if activity_date:
        payload["ActivityDate"] = activity_date
    if task_type:
        payload["Type"] = task_type
    if description:
        payload["Description"] = description
    if isinstance(extra_fields, dict):
        payload.update(extra_fields)
    try:
        result = sf_conn().Task.create(payload)
        return result
    except SalesforceMalformedRequest as e:
        return {"error": f"Task create failed: {e.content}", "payload": payload}
    except Exception as e:
        return {"error": f"Task create failed: {str(e)}", "payload": payload}


@mcp.tool
def search(search_string: str, ctx: Context):
    """Perform a Salesforce SOSL search."""
    if not isinstance(search_string, str) or not search_string.strip():
        return {"error": "Search string cannot be empty"}
    try:
        query_str = search_string
        if not search_string.strip().startswith("FIND"):
            query_str = f"FIND {{{search_string}}} IN ALL FIELDS RETURNING Account(Id, Name), Contact(Id, Name), Lead(Id, Name)"
        result = sf_conn().search(query_str)
        return {"results": result, "count": len(result)}
    except SalesforceMalformedRequest as e:
        return {"error": f"Search failed: {e.content}"}
    except Exception as e:
        return {"error": f"Search failed: {str(e)}"}

if __name__ == "__main__":
    mcp.run()

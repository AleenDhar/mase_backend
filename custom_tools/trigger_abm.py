"""Trigger a project run for a specific account under a BDR's app account.
Generic dispatch tool — can trigger ABM, Sales Intelligence, or any project."""

from langchain_core.tools import tool
import httpx
import os
import json


@tool
async def trigger_project(
    bdr_email: str,
    bdr_name: str,
    account_id: str,
    account_name: str,
    contacts_json: str,
    project_id: str = "",
    campaign_id: str = "",
    account_css_score: int = 0,
    account_tier: str = ""
) -> str:
    """Trigger a project run (e.g. ABM Outreach Engine) for a specific Salesforce account under a BDR's app account.
    
    Call this AFTER computing CBSM scores to launch full ABM outreach (research, email craft, Lemlist push) 
    for a top-priority account. The project runs asynchronously as a separate chat session under the 
    BDR's account in the app.

    WHEN TO CALL THIS:
    - After presenting the Top 30 scored accounts to the BDR
    - For accounts in Tier A or Tier B1 (CSS >= 50 or P0/P1 matrix cells)
    - Only for accounts where the BDR confirms they want ABM outreach (or if auto-dispatch is enabled)
    
    WHAT HAPPENS:
    1. This tool calls the Next.js app API
    2. The API creates a new chat under the BDR's account with the specified project
    3. The project engine receives the account ID, BD owner, and 5 enriched contacts
    4. It runs the full workflow asynchronously (e.g. 5-phase ABM workflow)
    5. Results appear in the BDR's chat history in the app

    Args:
        bdr_email: BDR's @zycus.com email address. This becomes the contactOwner for all Lemlist leads.
        bdr_name: BDR's full name as it appears in Salesforce (e.g. "Divya Deora").
        account_id: Salesforce Account ID (15 or 18 character format).
        account_name: Company name (e.g. "Lumen Technologies").
        contacts_json: JSON string of an array of exactly 5 enriched contacts. Each contact must have: rank (1-5), firstName, lastName, title, email, phone, linkedinUrl, companyName.
        project_id: The project ID to run. Pass the UUID of the target project (e.g. ABM project, Sales Intel project). If empty, the API will use its default project from env var.
        campaign_id: Lemlist campaign ID (e.g. "cam_xxxxxxxxxxxxxxxx"). If empty, the engine will ask for it.
        account_css_score: The CBSM Composite Score (0-100) for context.
        account_tier: The tier assignment (e.g. "Tier A", "Tier B1") for context.

    Returns:
        Success message with chat_id if dispatched, or error message if failed.
    """
    if not bdr_email or "@zycus.com" not in bdr_email.lower():
        return f"Error: bdr_email must be a valid @zycus.com address. Got: {bdr_email}"
    
    if not account_id or len(account_id) < 15:
        return f"Error: account_id must be a 15 or 18 character Salesforce ID. Got: {account_id}"
    
    try:
        contacts = json.loads(contacts_json) if isinstance(contacts_json, str) else contacts_json
        if not isinstance(contacts, list):
            return f"Error: contacts_json must be a JSON array. Got type: {type(contacts).__name__}"
        if len(contacts) != 5:
            return f"Error: Exactly 5 contacts required. Got: {len(contacts)}"
    except json.JSONDecodeError as e:
        return f"Error: contacts_json is not valid JSON: {e}"

    abm_message = f"""Run full ABM outreach for this account.

Salesforce Account ID: {account_id}
Account Name: {account_name}
CBSM Score: {account_css_score}/100 | {account_tier}

BD Owner: {bdr_name} |     {bdr_email}
(contactOwner for all Lemlist leads = {bdr_email})

{"Campaign ID: " + campaign_id if campaign_id else "Campaign ID: Not provided — you will need to ask for it at Phase 5."}

5 Enriched & Validated Contacts (ready for ABM — do NOT re-validate or re-enrich):
{json.dumps(contacts, indent=2)}

Execute the full 5-phase ABM workflow end-to-end:
Phase 1: Account Intelligence + Event History
Phase 2: LinkedIn Profile Research  
Phase 3: Research Synthesis
Phase 4: Email Craft + LinkedIn Message + Call Prep
Phase 5: Lemlist Push

All contacts are pre-validated. Do not re-enrich. Begin Phase 1 immediately."""

    endpoint = os.getenv("NEXTJS_APP_URL", "https://zycus-deal.vercel.app").strip()
    dispatch_secret = os.getenv("DISPATCH_SECRET", "").strip()
    
    try:
        payload = {
            "bdr_email": bdr_email,
            "bdr_name": bdr_name,
            "message": abm_message,
            "account_id": account_id,
            "account_name": account_name,
        }
        if project_id:
            payload["project_id"] = project_id

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{endpoint}/api/workflows/dispatch-abm",
                json=payload,
                headers={
                    "Authorization": f"Bearer {dispatch_secret}",
                    "Content-Type": "application/json",
                }
            )
            
            if resp.status_code == 200:
                data = resp.json()
                chat_id = data.get("chat_id", "unknown")
                used_project = data.get("project_id", project_id or "default")
                return (
                    f"✅ ABM dispatched successfully for {account_name} (CSS: {account_css_score}, {account_tier})\n"
                    f"   BDR: {bdr_name} ({bdr_email})\n"
                    f"   Chat ID: {chat_id}\n"
                    f"   Project: {used_project}\n"
                    f"   The engine is now running asynchronously under {bdr_name}'s account.\n"
                    f"   5 contacts will be researched, emails crafted, and pushed to Lemlist."
                )
            elif resp.status_code == 404:
                return f"❌ Dispatch failed: BDR {bdr_email} not found in the app."
            elif resp.status_code == 503:
                return f"⚠️ Dispatch failed: Server at capacity. Try again in a few minutes."
            else:
                return f"❌ Dispatch failed: HTTP {resp.status_code} — {resp.text[:500]}"
                
    except httpx.TimeoutException:
        return f"⚠️ Dispatch timed out for {account_name}."
    except httpx.ConnectError:
        return f"❌ Cannot reach {endpoint}. Check NEXTJS_APP_URL env var."
    except Exception as e:
        return f"❌ Dispatch error: {str(e)}"

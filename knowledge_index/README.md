# MASE Knowledge Index — Showpad sales library

These markdown files are the **knowledge cards** that form the searchable INDEX of the
Zycus Showpad sales library. The model is **Knowledge = index, Showpad = source**:

- The MASE knowledge base holds these cards (one per Showpad asset).
- An agent runs `search_knowledge` to find the right card for the topic / competitor /
  product, then treats **Showpad** as the authoritative source for the asset's contents.

Showpad folder (the actual source documents live here):
https://zycus.showpad.biz/webapp2/content/experiences/010902c77053177f1a6b63327ea42971/6c48b0d55b918c4bb8c74be1e0557bd568f72c1a0cb8167621fa30fc58fab20f

## How to load these into the knowledge base

Admin → Agent Control → **Knowledge** → **+ Add document** → drag-and-drop ALL of these
`.md` files at once (the uploader accepts multiple files). Each becomes its own document,
named by its filename. Set the **Type** for the batch (battlecards → `playbook`, decks →
`showpad_asset`). You can upload the battlecards as one batch (`playbook`) and the decks
as another (`showpad_asset`) so the `doc_type` is right per group.

## Card fields

Each card carries: `doc_type`, `one_liner`, `use_when`, `covers`, `citable_facts` (many
flagged **(verify)**), `showpad_assets`, `do_not`, `relates_to`.

**Treat every "(verify)" fact and every `do_not` item as UNCONFIRMED** — not citable in a
prospect-facing email until confirmed against a live source (Salesforce for customer
references, the actual Showpad asset / commercial team for figures and pricing). This is
how the index keeps the agent's anti-fabrication behaviour intact.

## Inventory

| File | doc_type | Status |
| --- | --- | --- |
| Zycus Integration Capabilities (SSO and Customer Cases) | showpad_asset | OK |
| Zycus SAP S4HANA Integration Deck | showpad_asset | OK |
| Zycus Support Packages | showpad_asset | OK |
| Zycus TAM-CAM Customer Support Model | showpad_asset | OK |
| iSwitch Change Management | showpad_asset | OK |
| Zycus Implementation Framework (UNAVAILABLE) | showpad_asset | empty extraction — human review |
| iSaaS Document (UNAVAILABLE) | other | encrypted — human review |
| Battlecard - Coupa vs Zycus | playbook | OK |
| Battlecard - GEP vs Zycus | playbook | OK |
| Battlecard - Ivalua vs Zycus | playbook | OK |
| Battlecard - Jaggaer vs Zycus | playbook | OK |
| Battlecard - SAP Ariba vs Zycus | playbook | OK |
| Battlecard - Zip vs Zycus | playbook | OK |

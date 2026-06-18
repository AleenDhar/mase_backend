# Zycus Integration Capabilities with SSO & Customer Cases

- **doc_type:** showpad_asset
- **one_liner:** A comprehensive presales deck covering Zycus's full integration architecture (iSaaS), file- and API-based integration patterns, SSO capabilities, the AppX low-code framework, and real customer integration case studies including Delta Air Lines and IFF.
- **use_when:** Prospect asks how Zycus integrates with their ERP or HR systems; IT/technical buyers or architects evaluate integration complexity; deal involves multi-ERP environments; prospect raises middleware cost concerns; SSO or identity management is a requirement; integration timeline or approach is challenged by a competitor.
- **covers:**
  - Zycus 25+ years and 32+ AI patents positioning slide; global customer verticals
  - iSaaS as single integration gateway for all Zycus modules (iSupplier, iContract, eProc, eInvoice, iMaster, TMS)
  - Two integration modes: file-based (XML/SFTP/zDOC schema) and API-based (JSON/REST)
  - iSaaS Lite vs. iSaaS Advance package comparison (feature matrix)
  - 1,000+ APIs across Zycus products (stated on slide)
  - GenAI-assisted data transformation, auto-mapping, complex orchestration
  - Integration architecture diagram: GenAI Transformation Engine, Smart Mapping Generation Engine, Recovery/Reconciliation Service, API Gateway, 1,121 granular APIs
  - Supported authentication: OAuth 2.0, 2-Factor/Basic, mSSL
  - Real-time connectors (REST, SOAP, ServiceBus) and batch connectors (SFTP, S3, Azure Blob, DataLake)
  - Standard integration touch points by module (iSupplier, iContract, iSource, eProc, eInvoice, TMS — with specific business entities and events)
  - Integration task distribution / RACI (Zycus vs. customer responsibilities)
  - Sample Workday and SAP integrations; Zycus iSaaS connectors for SnapLogic
  - GenAI Mapper demo walkthrough (Steps 1–Final)
  - Integration observability: dashboards, logging, monitoring, QA/compliance
  - AppX (Flexi SaaS Framework): low-code composable platform, third-party connectors (EcoVadis, D&B, CyberVadis, Mercateo, Amazon Business, invoice clearance apps for Mexico/Italy)
  - SSO enablement: ADFS, Ping Identity, ForgeRock, SiteMinder, Okta, Azure AD, IBM TIVOLI, OneLogin, NetIQ, and custom applications
  - User creation via TMS APIs integrated with customer HR system
  - Customer case — **Hyper Go-live (unnamed global company):** 82 OpCos, 60 countries, 3-year rollout; SI partners IBM & Accenture; $600M cumulative 4-year savings; USD 5Bn/9Bn annual spend; 300K–400K suppliers; 22K active contracts (verify customer name — not stated)
  - Customer case — **Delta Air Lines:** 80,000+ users, 6 global BUs, $54Bn annual revenue, $19Bn spend, 88,000+ suppliers, 25,000+ contracts; multi-ERP (SAP ECC + Lawson); 70,000+ users on SAP ECC, 10,000+ on Lawson
  - Customer case — **IFF (International Flavors & Fragrances):** $11.5Bn revenue, $3Bn+ spend, 4+ continents, 1,500+ users, 300,000+ suppliers, multi-SAP ECC integration (3 SAP ECC instances + Workday), M&A scope (DuPont N&B BU)
  - Documentation deliverables: HLD, LLD, SIT test case reports, post-go-live maintenance docs

- **citable_facts:**
  - Zycus has 1,000+ APIs across products for ERP integration (slide 7)
  - iSaaS architecture includes 1,121 granular APIs (slide 17)
  - Zycus supports file-based (XML/SFTP) and API-based (JSON/REST) integration natively; no middleware required (slides 6, 7)
  - iSaaS features: End-to-End GenAI data transformation, GenAI-assisted complex orchestrations, automated test framework, best-in-class error handling and recovery, any protocol/any format (slide 7)
  - Business impact claims: Fastest integration timelines, low resource investment, lower TCO, no middleware required, greater accountability, greater visibility and monitoring (slide 7)
  - Supported SSO identity providers include: ADFS, Ping Identity, ForgeRock, SiteMinder, Okta, Azure Active Directory, IBM TIVOLI, OneLogin, NetIQ, and custom applications (slide 44)
  - **Delta Air Lines:** 80,000+ users, 6 global BUs, $54Bn annual revenue, $19Bn of spend, 88,000+ suppliers, 25,000+ contracts (slide 31) — **(verify before citing; slide data may be point-in-time)**
  - **IFF (International Flavors & Fragrances):** $11.5Bn annual revenue, $3Bn+ spend, 4+ continents, 1,500+ users, 300,000+ suppliers, multi-ERP including 3 SAP ECC instances and Workday (slide 33) — **(verify)**
  - Hyper go-live customer: 82 OpCos across 60 countries, $600M cumulative 4-year savings, USD 5Bn–9Bn annual spend, SI partners IBM & Accenture — customer name **not stated in deck; do not name** **(verify all figures)**
  - AppX ecosystem integrations include EcoVadis, D&B, CyberVadis, Mercateo, Amazon Business (slide 18) — **(verify current availability)**
  - iSaaS Advance supports: data posting, data transformation, data orchestration, message retriggering, error reporting, one-to-many connections, ACK aggregation (slide 49)

- **showpad_assets:** This deck itself is the Showpad asset; pull alongside the iSaaS one-pager/datasheet if available on Showpad; Delta Air Lines case study if separately published.

- **do_not:**
  - Do not name the "Hyper go-live" customer — the deck shows their data but never identifies them by name
  - Do not cite the iSaaS customer "Vendor Board" slide (slide 8) — it is a visual-only slide with logos; customer names are not extractable from text
  - Do not represent AppX third-party integrations as certified partnerships without verification
  - Do not cite implementation timelines from this deck — they are not stated here (see Implementation Framework deck)
  - iSaaS.docx was found to be password-encrypted and could not be read; facts in this card are sourced from the PPTX only

- **relates_to:** Zycus SAP S/4HANA Integration Deck (SAP-specific path), iSaaS document (encrypted/unavailable), Zycus Implementation Framework, SAP Ariba battlecard (multi-ERP integration differentiation)

# ZoomInfo API Reference Guide

Complete parameter reference for all ZoomInfo MCP tools, validated against the live API.

## Authentication

- Method: PKI (Private Key Infrastructure)
- JWT claims: `aud=enterprise_api`, `iss=api-client@zoominfo.com`
- Token lifetime: ~1 hour (cached, auto-refreshed)
- Rate limiting: Exponential backoff with jitter on 429s
- Concurrency: Max 2 concurrent requests

---

## Tool Reference

### zi_search_contacts
Search for contacts in ZoomInfo database.

| Parameter | Type | API Field | Description |
|---|---|---|---|
| jobTitle | String | jobTitle | Job title. Use OR for multiple: `"CTO OR CIO"` |
| companyName | String | companyName | Company name |
| state | String | state | US state or region |
| country | String | country | Country name (e.g., `"United States"`) |
| department | String | department | Numeric ID (see enum below) |
| managementLevel | String | managementLevel | See enum below |
| revenueMin | Integer | revenueMin | Min revenue in thousands USD (1000000 = $1B) |
| revenueMax | Integer | revenueMax | Max revenue in thousands USD |
| employeeCount | String | employeeCount | See enum below |
| industryKeywords | String | industryKeywords | Keywords with AND/OR operators |
| page | Integer | page | Page number (default 1) |
| rpp | Integer | rpp | Results per page (default 25) |

### zi_enrich_contact
Enrich a contact with full profile data. Costs 1 credit per new record.

| Parameter | Type | API Field | Notes |
|---|---|---|---|
| personId | String | personId | ZoomInfo person ID |
| emailAddress | String | emailAddress | Work email |
| firstName | String | firstName | Use with lastName + companyName |
| lastName | String | lastName | Use with firstName + companyName |
| companyName | String | companyName | Company name for matching |
| companyDomain | String | companyDomain | Company domain for matching |

**Output fields returned:** id, firstName, lastName, email, phone, jobTitle, city, state, country, managementLevel, companyName, companyId, mobilePhone

### zi_search_companies
Search for companies in ZoomInfo database.

| Parameter | Type | API Field | Description |
|---|---|---|---|
| companyName | String | companyName | Company name |
| companyWebsite | String | companyWebsite | Domain (e.g., `"microsoft.com"`) |
| industryKeywords | String | industryKeywords | Keywords with AND/OR: `"software AND security"` |
| revenue | String | revenue | Revenue range enum (see below) |
| revenueMin | Integer | revenueMin | Min revenue in thousands USD |
| revenueMax | Integer | revenueMax | Max revenue in thousands USD |
| employeeCount | String | employeeCount | Employee count enum (see below) |
| state | String | state | US state |
| country | String | country | Country name |
| techAttributeTagList | String | techAttributeTagList | Technology product tags |
| page | Integer | page | Page number |
| rpp | Integer | rpp | Results per page |

### zi_enrich_company
Enrich a company with firmographic data. Costs 1 credit per new record.

| Parameter | Type | API Field | Notes |
|---|---|---|---|
| companyId | String | companyId | ZoomInfo company ID |
| companyName | String | companyName | Company name |
| companyDomain | String | companyWebsite | Domain (mapped to companyWebsite internally) |

**Output fields returned:** id, name, website, revenue, employeeCount, city, state, country, foundedYear, ticker, phone, street, zipCode

### zi_get_scoops
Search for buying signals, leadership changes, expansions, funding.

| Parameter | Type | API Field | Description |
|---|---|---|---|
| scoopTopic | String | scoopTopic | Comma-separated topic IDs (see Scoop Topics below) |
| companyId | String | companyId | ZoomInfo company ID |
| companyName | String | companyName | Company name |
| companyWebsite | String | companyWebsite | Company domain |
| publishedStartDate | String | publishedStartDate | Start date `YYYY-MM-DD` |
| publishedEndDate | String | publishedEndDate | End date `YYYY-MM-DD` |
| country | String | country | Country filter |
| page | Integer | page | Page number |
| rpp | Integer | rpp | Results per page |

### zi_get_intent
Get intent signals showing companies researching specific topics.

**ACCOUNT LIMITATION:** Company-level filters (`companyId`, `companyWebsite`) are **NOT supported** on this account and will return 400 Bad Request with `invalidInputFields`. To find intent for a specific company, retrieve topic-based results and filter client-side by company name.

| Parameter | Type | API Field | Description |
|---|---|---|---|
| topics | Array[String] | topics | **REQUIRED.** Array of topic name strings (from zi_list_intent_topics) |
| audienceStrengthMin | String | audienceStrengthMin | Min strength (weaker end): C, D, or E |
| audienceStrengthMax | String | audienceStrengthMax | Max strength (stronger end): A or B |
| signalScoreMin | Integer | signalScoreMin | Min score 60-100 |
| country | String | country | Country filter |
| page | Integer | page | Page number |
| rpp | Integer | rpp | Results per page |

**Important:**
- `topics` must contain exact topic name strings from the account's subscribed topics. Call `zi_list_intent_topics` first to get the list.
- `audienceStrengthMin` should be the **weaker** end (e.g., C) and `audienceStrengthMax` should be the **stronger** end (e.g., A). A is strongest, E is weakest.

### zi_list_intent_topics
Lists all intent topics the account is subscribed to. Returns topic name strings to use with `zi_get_intent`.

No parameters required.

### zi_get_news
Get company news articles. **ACCOUNT LIMITATION: Returns 403 Forbidden - this account does NOT have News API entitlement.** Contact ZoomInfo Account Manager to enable. Use `apollo_search_news_articles` or `duckduckgo_search` as alternatives.

| Parameter | Type | API Field | Description |
|---|---|---|---|
| companyId | String | companyId | ZoomInfo company ID |
| companyName | String | companyName | Company name |
| companyWebsite | String | companyWebsite | Company domain |
| pageDateMin | String | pageDateMin | Earliest date `YYYY-MM-DD` |
| pageDateMax | String | pageDateMax | Latest date `YYYY-MM-DD` |
| page | Integer | page | Page number |
| rpp | Integer | rpp | Results per page |

### zi_get_technologies
Get the technology stack used by a company.

| Parameter | Type | API Field | Description |
|---|---|---|---|
| companyId | String | companyId | ZoomInfo company ID (required) |

**Endpoint:** `/enrich/tech` (NOT `/lookup/technology`)

### zi_get_org_chart
Get organizational hierarchy / reporting structure.

| Parameter | Type | API Field | Description |
|---|---|---|---|
| companyId | String | companyId | ZoomInfo company ID (required) |

**Endpoint:** `/enrich/orgchart` (NOT `/lookup/orgchart`)

---

## Enum Reference

### Management Level
| Value | Description |
|---|---|
| `C Level Exec` | C-Suite executives |
| `VP Level Exec` | Vice Presidents |
| `Director` | Directors |
| `Manager` | Managers |
| `Non Manager` | Individual contributors |
| `Board Member` | Board members |

### Department (Numeric IDs)
| ID | Name |
|---|---|
| 0 | C-Suite |
| 1 | Finance |
| 2 | Human Resources |
| 3 | Sales |
| 4 | Operations |
| 5 | Information Technology |
| 6 | Engineering & Technical |
| 7 | Marketing |
| 8 | Legal |
| 9 | Medical & Health |
| 10 | Other |

### Employee Count
| Value | Range |
|---|---|
| `1to4` | 1 - 5 |
| `5to9` | 5 - 10 |
| `10to19` | 10 - 20 |
| `20to49` | 20 - 50 |
| `50to99` | 50 - 100 |
| `100to249` | 100 - 250 |
| `250to499` | 250 - 500 |
| `500to999` | 500 - 1,000 |
| `1000to4999` | 1,000 - 5,000 |
| `5000to9999` | 5,000 - 10,000 |
| `10000plus` | Over 10,000 |

### Revenue Range
| Value | Upper Bound |
|---|---|
| `under500k` | Under $500K |
| `500kto1m` | $500K - $1M |
| `1mto5m` | $1M - $5M |
| `5mto10m` | $5M - $10M |
| `10mto25m` | $10M - $25M |
| `25mto50m` | $25M - $50M |
| `50mto100m` | $50M - $100M |
| `100mmto250m` | $100M - $250M |
| `250mto500m` | $250M - $500M |
| `500mto1g` | $500M - $1B |
| `1gto5g` | $1B - $5B |
| `5gplus` | Over $5B |

Alternatively, use `revenueMin` / `revenueMax` (in thousands USD) for custom ranges.

### Scoop Topic IDs (Key Topics)
| ID | Topic |
|---|---|
| 1 | Cloud |
| 9 | CRM |
| 10 | Pain Point |
| 14 | Executive Moves |
| 17 | New Hire |
| 18 | Promotion |
| 19 | Seeking Replacement |
| 26 | Mergers & Acquisitions |
| 27 | Earnings |
| 33 | Hiring Plans |
| 34 | Facilities Relocation/Expansion |
| 41 | Spending/Investment |
| 52 | Information Security |
| 84 | Personnel Moves |
| 100 | Left Company |
| 107 | Layoffs |
| 117 | Funding |
| 119 | Machine Learning |
| 131 | Bankruptcy |
| 136 | Cyber Security |
| 305 | Revenue Operations |

Full list: 300+ topics available. Use the `/lookup/scooptopic` endpoint for the complete list.

### Intent Topics (Account-Specific)
Intent topics are specific to each account's subscription. Current subscribed topics include:
- Adobe Sign, Artificial Intelligence Platforms, Carbon Management, Certificate Authority (CA), Conga, Coupa, DocuSign, E-Signature Software, Electronic Document, ESG, GEP Worldwide, Global Procurement, HelloSign, PandaDoc, and more.

Call `zi_list_intent_topics` to get the full current list.

---

## Common Mistakes & Fixes

| Mistake | Fix |
|---|---|
| `locationCountry` | Use `country` |
| `locationState` | Use `state` |
| `industry` as free text | Use `industryKeywords` with AND/OR operators |
| `resultsPerPage` | Use `rpp` |
| `companyDomain` in search | Use `companyWebsite` |
| `publishedDateFrom/To` in scoops | Use `publishedStartDate`/`publishedEndDate` |
| `topicId` in scoops | Use `scoopTopic` (comma-separated string) |
| `topicId` in intent | Use `topics` (Array of topic name strings) |
| `audienceStrength` in intent | Use `audienceStrengthMin`/`audienceStrengthMax` (A-E) |
| `/lookup/technology` path | Use `/enrich/tech` |
| `/lookup/orgchart` path | Use `/enrich/orgchart` |
| Revenue as `$1B+` text | Use enum `5gplus` or `revenueMin: 1000000` |
| Employee count as `10,001+` | Use enum `10000plus` |

---

## Entitlement Notes
- **Search** endpoints are free (no credits consumed)
- **Enrich** endpoints cost 1 credit per new record (free for 12 months after first enrich)
- **News** requires separate News entitlement (403 if not entitled)
- **Intent** requires intent topic subscriptions configured in ZoomInfo platform
- **Technologies** and **Org Chart** use enrich endpoints (may consume credits)

---

## Credit Model
| Action | Credit Cost |
|---|---|
| Search (contacts, companies, scoops, intent) | Free |
| Enrich (contact, company, tech, orgchart) | 1 credit per new record |
| Re-enrich within 12 months | Free |
| Lookup endpoints | Free |

---

## API Base URL
```
https://api.zoominfo.com
```

## Correct Endpoint Paths
| Tool | Path | Method |
|---|---|---|
| zi_search_contacts | `/search/contact` | POST |
| zi_search_companies | `/search/company` | POST |
| zi_enrich_contact | `/enrich/contact` | POST |
| zi_enrich_company | `/enrich/company` | POST |
| zi_get_scoops | `/search/scoop` | POST |
| zi_get_intent | `/search/intent` | POST |
| zi_get_news | `/search/news` | POST |
| zi_get_technologies | `/enrich/tech` | POST |
| zi_get_org_chart | `/enrich/orgchart` | POST |
| zi_list_intent_topics | `/lookup/intent/topics` | GET |
| Authentication | `/authenticate` | POST |

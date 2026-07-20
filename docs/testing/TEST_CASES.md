# Test Cases

Use this checklist for integration testing, UAT, and regression planning. Automated tests should use synthetic fixtures and mocks unless a test explicitly states that an approved test environment is required.

| Test ID | Description | Preconditions | Steps | Expected Result | Pass/Fail |
| --- | --- | --- | --- | --- | --- |
| TC-001 | Standard invoice parses and maps | Valid synthetic UBL XML with one line, one supplier, one customer, one VAT tax | Parse XML; run mapping preview | Normalized header, parties, totals, tax, and line are extracted; mapping status is `ready` when required values exist |  |
| TC-002 | Multi-line invoice | Valid UBL XML with multiple invoice lines | Parse XML; run mapping preview | Every line is preserved with sequence, description, quantity, unit price, unit code, tax, and line total |  |
| TC-003 | Foreign currency invoice | Valid UBL XML with non-TRY document currency | Parse XML; run mapping preview; run resolution with matching currency fixture | Currency is preserved as ISO code; missing or inactive currency is reported by resolution |  |
| TC-004 | Tax variations | UBL XML includes different tax percentages or tax categories | Parse XML; run mapping preview; run resolution with exact tax fixtures | Tax totals and subtotals are preserved; exact purchase tax matches resolve; unknown tax remains unresolved |  |
| TC-005 | Discounts and allowances | UBL XML includes invoice-level or line-level allowances | Parse XML; run mapping preview | Allowance amounts are preserved in normalized totals or line allowances; unsupported mapping details become warnings |  |
| TC-006 | Recurring vendor | Vendor already exists in Odoo fixtures by VAT/VKN | Run mapping preview; run Odoo resolution | Partner resolves deterministically by exact VAT/VKN |  |
| TC-007 | New vendor | Vendor does not exist in Odoo fixtures | Run Odoo resolution | Partner status is unresolved; no partner is created |  |
| TC-008 | Missing product | Line product does not match existing Odoo product fixture | Run Odoo resolution | Product is unresolved; draft creation is blocked until reviewed ids are supplied |  |
| TC-009 | Missing tax | Invoice tax does not match existing Odoo tax fixture | Run Odoo resolution | Tax is unresolved; mapping remains reviewable; no tax is created |  |
| TC-010 | Missing journal | No configured or matching purchase journal exists | Run Odoo resolution | Journal is unresolved or invalid; draft creation is blocked |  |
| TC-011 | Duplicate UUID / ETTN during metadata sync | Existing invoice metadata row with same provider, direction, and ETTN | Run sync for same invoice again | Existing row is updated or skipped; duplicate metadata row is not created |  |
| TC-012 | Duplicate UUID / ETTN during draft creation | Existing local draft invoice record for the same ETTN | Submit same reviewed preview again | Existing draft reference is returned; no second Odoo create call occurs |  |
| TC-013 | Empty XML | Stored document has zero bytes or only whitespace | Parse document | Parser returns safe malformed XML or missing content error; full XML is not logged |  |
| TC-014 | Malformed XML | Stored document contains invalid XML syntax | Parse document | Parser returns safe malformed XML error with document id/storage key diagnostics |  |
| TC-015 | Unsupported document | Document metadata type is not `UBL_XML` | Parse document | Parser rejects unsupported document type; no provider or Odoo calls occur |  |
| TC-016 | Cancelled invoice metadata, if provider returns status | Uyumsoft list item includes a cancelled-like status in test data | Sync metadata; inspect normalized summary | Status is preserved as provider status/extra metadata; no cancellation or status mutation is called |  |
| TC-017 | Missing optional party contact | UBL XML omits contact details | Parse XML | Parser succeeds; optional contact fields are empty/null |  |
| TC-018 | Invalid decimal | UBL XML contains non-decimal monetary value | Parse XML | Parser returns invalid decimal error with safe field/path |  |
| TC-019 | Invalid date/time | UBL XML contains invalid issue date or time | Parse XML | Parser returns invalid date/time error with safe field/path |  |
| TC-020 | Ambiguous partner | Odoo fixture returns multiple exact name candidates and no VAT match | Run Odoo resolution | Partner status is ambiguous; no candidate is auto-selected |  |
| TC-021 | Ambiguous product | Odoo fixture returns multiple exact name candidates and no default code match | Run Odoo resolution | Product status is ambiguous; draft creation is blocked |  |
| TC-022 | XML attachment already downloaded | Existing invoice document row has same invoice id, document type, and hash | Request UBL XML download again | Existing document metadata is returned idempotently; file is not duplicated |  |
| TC-023 | XML attachment content conflict | Existing invoice document row has same invoice id/type but different hash | Request UBL XML download again | Conflict is reported safely; existing file is not overwritten silently |  |
| TC-024 | Provider timeout during sync | Uyumsoft connector mock raises timeout | Run sync | Sync run is failed with safe timeout category; completed prior pages remain idempotent |  |
| TC-025 | Odoo timeout during resolution or draft creation | Odoo client mock raises timeout | Run resolution or draft creation | Safe connector error is returned; no secrets or payloads are logged |  |
| TC-026 | Database unavailable | Database connection is unavailable | Run readiness or workflow | Readiness fails or workflow returns safe persistence error; no external write is attempted after a local precondition failure |  |
| TC-027 | Storage unavailable | Document storage root is not writable/readable | Run readiness or document workflow | Readiness or document operation fails safely; XML content is not logged |  |
| TC-028 | Production gate disabled | `APP_ENV=production` without required explicit approvals | Start app or readiness validation | Startup/readiness rejects unsafe configuration with safe setting names only |  |
| TC-029 | Draft creation without confirmation | Reviewed preview is submitted without `confirm_create_draft=true` | Call draft endpoint | Request is rejected; no Odoo call occurs |  |
| TC-030 | Read-only operation enforcement | Test doubles expose forbidden methods | Run sync, document download, parser, mapping, and resolution tests | Forbidden Uyumsoft and Odoo operations are never called |  |

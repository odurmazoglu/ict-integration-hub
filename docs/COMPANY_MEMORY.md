# Company Memory

Company Memory is the accepted context layer for ICT IPP. It provides historical company-specific context to rules, review workflows, and AI Advisor without becoming the decision authority.

No Company Memory runtime is currently implemented in this repository.

## Purpose

Company Memory may help the platform remember:

- vendor aliases and known identifiers
- prior review decisions
- procurement relationship patterns
- common missing-data cases
- preferred traceability links
- historical exceptions
- user-approved normalization hints

## Authority Boundary

Company Memory is evidence, not authority.

It may inform:

- Rule Engine inputs
- AI Advisor explanations
- Import Workbench review hints
- audit context

It must not:

- override deterministic rules
- auto-select ambiguous candidates
- authorize ERP writes
- mutate provider or ERP state
- become a hidden source of business logic

## Company Memory And AI

AI Advisor may be Company Memory aware. Any memory used by AI should be referenceable in the recommendation output.

If AI uses Company Memory, the recommendation must still be marked advisory. Deterministic rules and user review remain authoritative.

## Data Governance

Future implementation must define:

- what can be stored
- retention rules
- review/approval rules for memory writes
- privacy constraints
- tenant/company isolation
- redaction and logging rules
- deletion or correction process
- audit trail

Do not store credentials, API keys, SOAP payloads, full raw XML, or full ERP payloads as memory.

## Candidate Memory Types

| Type | Example | Decision role |
| --- | --- | --- |
| Vendor alias | Supplier legal name vs short name | Review hint only unless deterministic rule approves |
| Product mapping note | Previously reviewed seller code | Rule input only after approved memory governance exists |
| Traceability pattern | Vendor bill usually linked to a project/customer asset | Suggestion or review hint |
| Exception note | Vendor uses unusual tax category | Advisory explanation |

## Related Documents

- [AI Advisor](AI_ADVISOR.md)
- [Import Session](IMPORT_SESSION.md)
- [Company Memory ADR](adr/ADR-0007-company-memory.md)

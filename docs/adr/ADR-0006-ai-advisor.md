# ADR-0006: AI Advisor

- Status: Accepted
- Date: 2026-07-30

## Context

AI can help explain imports, recommend review actions, and use company context. It can also create unacceptable financial risk if it is allowed to make decisions or mutate ERP/provider state.

## Decision

AI Advisor:

- never makes business decisions
- produces recommendations only
- runs only after Rule Engine
- uses local AI through Ollama when implemented
- may be Company Memory aware

## Consequences

AI output must be clearly marked advisory. It must not select workflows, approve imports, choose ambiguous matches, or execute provider/ERP operations.

## Related Documentation

- [AI Advisor](../AI_ADVISOR.md)
- [Company Memory](../COMPANY_MEMORY.md)

# ADR-0004: Local AI Advisor Is Advisory Only

- Status: Accepted
- Date: 2026-07-21

## Context

Some invoices will not match a deterministic rule. The platform should help users by learning from prior approved decisions without allowing probabilistic output to create accounting records autonomously.

## Decision

AI is an advisor, not a decision authority.

Execution order:

1. deterministic matching
2. deterministic Rule Engine
3. company-memory retrieval
4. local AI recommendation
5. user or approved deterministic policy decision

The default AI deployment uses a local Ollama-compatible model. Company data must not be sent to a token-based external model by default.

AI output must include a recommendation, confidence indicator, and human-readable rationale. It cannot call ERP write services.

## Consequences

- AI mistakes cannot directly create ERP records.
- The platform remains usable without AI.
- Approved user decisions can become deterministic rules.
- Company Memory may use pgvector to retrieve similar historical cases.

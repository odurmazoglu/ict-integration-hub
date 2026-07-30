# ADR-0005: Rule Engine

- Status: Accepted
- Date: 2026-07-30

## Context

IPP automation must be deterministic, auditable, and safe before advisory AI is introduced.

## Decision

Rule Engine lives inside ICT IPP.

It is:

- deterministic
- executed before AI
- the source of workflow decisions

AI Advisor may consume Rule Engine results but may not override them.

## Consequences

Current deterministic matching, idempotency, validation, production gates, and Odoo resolution rules are part of the rule foundation. Future consolidated Rule Engine work must preserve existing rule behavior unless a new accepted ADR changes it.

## Related Documentation

- [Rule Engine](../RULE_ENGINE.md)
- [Matching](../MATCHING.md)

# Contributing

Contributions to ICT Integration Hub must preserve the accepted ICT IPP architecture and safety boundaries.

## Before You Start

Read:

- [Project Constitution](PROJECT_CONSTITUTION.md)
- [Architecture](ARCHITECTURE.md)
- [Architecture Decisions](adr/README.md)
- [Development Workflow](DEVELOPMENT_WORKFLOW.md)
- [Coding Standards](CODING_STANDARDS.md)

Then confirm whether the change is documentation-only, implementation, migration, operational, or provider-facing.

## Contribution Rules

- Keep changes narrow and issue-backed.
- Do not redesign accepted architecture inside an unrelated PR.
- Add or update tests for implementation behavior.
- Update docs when behavior, boundaries, workflows, or operations change.
- Add an ADR for durable architectural decisions.
- Do not commit secrets or real environment files.
- Do not add dependencies without justification.

## Safety Boundaries

Do not call or implement Uyumsoft state-changing operations by default:

- `SetInvoicesTaken`
- `SendInvoice`
- `Cancel*`
- `RetrySendInvoices`
- `MoveToDraftStatus`
- acknowledgement or status mutation operations

Do not call or implement Odoo accounting mutations by default:

- `action_post`
- unlink
- payment registration
- reconciliation
- automatic partner/product/tax creation

## AI And Generated Code

AI assistants and code generation tools must follow the same architecture:

- deterministic rules before AI
- AI advisory only
- Odoo as adapter
- Hub owns decisions
- ERP-independent domain logic
- repository boundaries for ERP data
- no hidden provider writes

Generated code must be reviewed and tested like handwritten code.

## Pull Request Expectations

PRs should include:

- summary
- scope
- related issue
- validation results
- Docker health result when applicable
- migration/rollback notes
- security impact
- known limitations
- remaining risks

Documentation-only PRs should explicitly state that runtime behavior, API behavior, database schema, tests, dependencies, and provider connections were not changed.

## Related Documents

- [Development Workflow](DEVELOPMENT_WORKFLOW.md)
- [Coding Standards](CODING_STANDARDS.md)
- [Testing Documentation](testing/README.md)

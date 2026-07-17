# Contributing

This repository follows the delivery rules in `AGENTS.md`. Keep work small, issue-backed, and safe by default.

## Expected Workflow

```text
Issue
-> Milestone
-> Labels
-> Branch
-> Draft PR
-> CI
-> Review
-> Merge
```

1. Start with a GitHub issue that defines objective, scope, safety boundaries, acceptance criteria, dependencies, labels, and milestone.
2. Assign the issue to one milestone and the smallest useful label set.
3. Create a branch from latest `main`.
4. Open a Draft PR early enough for CI visibility.
5. Keep CI green before requesting review.
6. Merge only after scope, safety boundaries, tests, Docker validation, migration notes, rollback, limitations, and remaining risks are clear.

## Milestones

Expected milestones:

- `Foundation`: bootstrap, CI, Docker, WSDL discovery, read-only probes, read-only listing, and metadata persistence. This can be closed once those merged PRs are accepted as complete.
- `Invoice Sync`: incremental sync, sync run tracking, safe scheduling, idempotent refresh behavior.
- `Attachments`: XML/UBL download, UBL parser, attachment storage, parse diagnostics.
- `Odoo Integration`: read-only mapping preview, controlled Odoo draft invoice creation, Odoo idempotency.
- `Production Ready`: production gates, runbooks, monitoring, backup/restore, operational security, go-live checklist.

Current automation note: the available GitHub connector can create issues and PRs, but does not expose milestone creation. If milestones do not exist, create them manually in GitHub and assign the roadmap issues listed in the README.

## Labels

Use this label set consistently:

- `feature`
- `bug`
- `enhancement`
- `refactor`
- `documentation`
- `security`
- `database`
- `uyumsoft`
- `odoo`
- `ci`
- `testing`
- `blocked`

Avoid duplicates such as `test` vs `testing`, `db` vs `database`, or `docs` vs `documentation`.

Current automation note: the available GitHub connector can apply existing labels but does not expose label creation. If labels do not exist, create the set above manually before assigning roadmap issues.

## Branches

Use descriptive branches:

```text
codex/<short-task-name>
```

Examples:

- `codex/incremental-sync-engine`
- `codex/ubl-parser`
- `codex/odoo-mapping-preview`

## Pull Requests

Every PR must include:

- Summary
- Scope
- Related issue or `Closes #...`
- Tests executed
- Docker validation
- Security boundaries
- Migration details
- Rollback
- Known limitations
- Remaining risks

Keep PRs as Draft until local validation is complete and CI is green.

## Required Validation

Run the required AGENTS validation unless the PR is explicitly documentation-only and the maintainer approves a narrower validation:

```bash
ruff check .
ruff format --check .
pytest
docker compose down --remove-orphans
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8000/health
docker compose exec api pytest
docker compose exec api ruff check .
docker compose logs api
docker compose logs db
```

If migrations are present:

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

## Safety Boundaries

- Never commit `.env` files, credentials, API keys, or real secret values.
- Stay in the Uyumsoft test environment unless a specific approved issue changes that boundary.
- Do not implement or call `SetInvoicesTaken`, `SendInvoice`, `Cancel*`, `RetrySendInvoices`, `MoveToDraftStatus`, or any status-changing Uyumsoft SOAP operation.
- Do not call Odoo create/write/unlink/action_post unless the issue explicitly authorizes it.
- Do not log credentials, XML/PDF payloads, or full invoice contents.

## Optional GitHub Projects

GitHub Projects are optional. Do not add one unless permissions are available and configuration can be completed reliably.

Suggested manual Project setup:

- Project name: `ICT Integration Hub Roadmap`
- Fields: `Status`, `Milestone`, `Area`, `Risk`, `Target`
- Views:
  - Roadmap by milestone
  - Active sprint
  - Blocked work
  - Production readiness

Initial statuses:

- Backlog
- Ready
- In Progress
- In Review
- Done
- Blocked

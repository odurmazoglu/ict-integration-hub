# Development Workflow

ICT Integration Hub work should be issue-backed, branch-based, reviewed, and production-safe.

## Standard Flow

```text
Issue -> Milestone -> Labels -> Branch -> Draft PR -> CI -> Review -> Merge
```

1. Read the issue, relevant ADRs, and affected docs.
2. Create a dedicated branch from the intended base.
3. Keep the change as small as the goal allows.
4. Implement code, migrations, tests, and docs together when behavior changes.
5. Run local validation.
6. Commit with a meaningful message.
7. Push the branch.
8. Open a draft PR against `main`.
9. Include scope, tests, Docker validation, security boundaries, migration/rollback notes, limitations, and related issue.

## Documentation-Only Work

Documentation-only PRs must not:

- modify application source code
- modify tests except documentation references when explicitly needed
- change API behavior
- change database schema
- add dependencies
- connect to Uyumsoft or Odoo
- refactor runtime code

Validation should still include Markdown review and repository checks that do not require external providers. If the maintainer requires full validation, run the standard commands.

## Application-Layer Work

Application-layer PRs should establish or extend orchestration boundaries without moving business behavior unless the issue explicitly asks for behavior implementation.

- Add commands for state-changing use-case input.
- Add queries for read-only use-case input.
- Add immutable application DTOs for use-case results.
- Add ports for future infrastructure dependencies.
- Add use cases only when they coordinate a real workflow.
- Keep infrastructure implementations outside `app/application`.

See [Application Layer](APPLICATION_LAYER.md) for the accepted foundation.

## Required Local Validation

Default validation:

```bash
ruff check .
ruff format --check .
pytest
```

Docker validation:

```bash
docker compose down --remove-orphans
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8000/health
```

If migrations are included:

```bash
alembic upgrade head
```

## Branches

Use:

```text
codex/<short-task-name>
```

Keep branch names descriptive and task-scoped.

## Pull Request Checklist

Every PR should explain:

- purpose and scope
- related issue or `Closes #...`
- user-visible behavior
- tests run and results
- Docker validation and health check result
- security boundaries
- migration and rollback
- limitations and remaining risks

Draft PRs are expected until validation is complete.

## Working With Dirty Trees

Do not overwrite unrelated changes. If unrelated changes exist, keep the task scoped and preserve user work.

## Related Documents

- [Contributing](CONTRIBUTING.md)
- [Coding Standards](CODING_STANDARDS.md)
- [Project Constitution](PROJECT_CONSTITUTION.md)

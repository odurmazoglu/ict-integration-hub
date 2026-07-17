## Summary

-

## Scope

-

## Related issue / Closes

- Closes #

## Tests executed

- [ ] `pytest`
- [ ] `ruff check .`
- [ ] `ruff format --check .`

## Docker validation

- [ ] `docker compose down --remove-orphans`
- [ ] `docker compose up --build -d`
- [ ] `docker compose ps`
- [ ] `curl --fail http://localhost:8000/health`
- [ ] `docker compose exec api pytest`
- [ ] `docker compose exec api ruff check .`
- [ ] `docker compose logs api` inspected
- [ ] `docker compose logs db` inspected

## Security boundaries

- [ ] No secrets, credentials, `.env` contents, XML/PDF payloads, or full invoice data are logged or committed.
- [ ] Uyumsoft test environment boundary is preserved.
- [ ] No forbidden Uyumsoft status-changing operation is implemented or called.
- [ ] No Odoo create/write/unlink/action_post is introduced unless explicitly in scope.

## Migration details

- Migration required: yes/no
- Migration commands run:
  - [ ] `alembic upgrade head`
  - [ ] `alembic downgrade -1`
  - [ ] `alembic upgrade head`

## Rollback

-

## Known limitations

-

## Remaining risks

-

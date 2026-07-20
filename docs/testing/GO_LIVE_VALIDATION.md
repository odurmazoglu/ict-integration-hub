# Go-Live Validation

Use this checklist before enabling production operations. Production must remain disabled until all mandatory approvals and gates are complete.

## Build And CI

- [ ] CI is green for the exact commit.
- [ ] `ruff check .` passed.
- [ ] `ruff format --check .` passed.
- [ ] `pytest` passed.
- [ ] Docker build/start validated where required.
- [ ] Health endpoint verified.
- [ ] API and DB logs inspected for startup errors and secret redaction.

## UAT And Business Approval

- [ ] UAT approved by Finance.
- [ ] Technical Owner approved UAT results.
- [ ] Project Owner approved UAT results.
- [ ] Known limitations reviewed with stakeholders.
- [ ] Duplicate prevention verified.
- [ ] Draft-only Odoo behavior verified.

## Production Configuration

- [ ] `APP_ENV=production` is intentional.
- [ ] `PRODUCTION_OPERATIONS_ENABLED=true` is intentional.
- [ ] `PRODUCTION_APPROVAL_ACK=APPROVED_FOR_PRODUCTION` is set only after approval.
- [ ] `UYUMSOFT_ENVIRONMENT=production` is approved.
- [ ] Uyumsoft production WSDL host is approved.
- [ ] Uyumsoft test WSDL does not point to production.
- [ ] Odoo base URL is the approved production URL.
- [ ] Database URL does not point to localhost.
- [ ] Placeholder credentials are not used.
- [ ] Required environment variables are configured through secret storage.

## Secrets And Access

- [ ] `.env` files are untracked.
- [ ] No secrets are committed.
- [ ] Uyumsoft credentials are least privilege for listing and document retrieval only.
- [ ] Uyumsoft status-changing permissions are not required for current scope.
- [ ] Odoo user has only approved permissions.
- [ ] Odoo posting permission is not required for Integration Hub.
- [ ] Odoo unlink/master-data mutation permissions are not required for Integration Hub.

## Monitoring And Operations

- [ ] Monitoring approach is agreed with owners.
- [ ] `/health/ready` is monitored by the deployment platform or operations process.
- [ ] Sync failures and retry counts have an observation path.
- [ ] Document download failures have an observation path.
- [ ] Parser failures have an observation path.
- [ ] Odoo resolution missing/ambiguous counts have an observation path.
- [ ] Draft creation failures have an observation path.
- [ ] Escalation owner is identified.

Monitoring is an operational requirement. Do not claim a specific monitoring integration exists unless it has been implemented and deployed separately.

## Backup And Rollback

- [ ] PostgreSQL backup completed.
- [ ] Backup restore procedure reviewed or tested.
- [ ] Document storage backup completed where documents exist.
- [ ] Alembic current revision recorded.
- [ ] Rollback owner identified.
- [ ] Previous approved image or commit is available.
- [ ] Manual reconciliation procedure reviewed for external Odoo write followed by local persistence failure.
- [ ] Rollback limitations are understood: database rollback does not remove Odoo draft records already created.

## Final Approval

- [ ] Production approval completed.
- [ ] Finance approval recorded.
- [ ] Technical approval recorded.
- [ ] Project approval recorded.
- [ ] Go-live window agreed.
- [ ] Support coverage confirmed.

## Go / No-Go Decision

Decision: Go / No-Go

Reason:

Approvers:

| Role | Name | Decision | Date | Notes |
| --- | --- | --- | --- | --- |
| Finance |  | Go / No-Go |  |  |
| Technical Owner |  | Go / No-Go |  |  |
| Project Owner |  | Go / No-Go |  |  |

Open blockers:

| Blocker | Owner | Required Action | Due Date |
| --- | --- | --- | --- |
|  |  |  |  |

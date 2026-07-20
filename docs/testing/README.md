# Testing Documentation

This package defines how ICT Integration Hub should be validated during integration testing, UAT, production readiness, go-live, and future maintenance.

It is documentation only. The checklists and scenarios describe how to validate the current system without changing runtime behavior, adding features, or connecting to external providers unless an explicitly approved manual test phase requires it.

## Purpose

The testing package gives technical and finance stakeholders a shared validation language for:

- read-only Uyumsoft invoice metadata sync
- UBL XML document download and local storage
- local UBL parser behavior
- Odoo mapping preview
- read-only Odoo resolution
- draft-only Odoo vendor bill creation
- production readiness gates and rollback preparation

It does not replace automated tests. It explains when to use automated tests, synthetic fixtures, mocked providers, controlled staging validation, and user sign-off.

## Testing Phases

Use the phase documents in this order:

1. [Test Plan](TEST_PLAN.md): end-to-end validation process and phase success criteria.
2. [Test Cases](TEST_CASES.md): practical scenario checklist for parser, mapping, resolution, draft creation, idempotency, and invalid inputs.
3. [Edge Cases](EDGE_CASES.md): uncommon conditions and expected behavior.
4. [Failure Injection](FAILURE_INJECTION.md): controlled failure testing and recovery expectations.
5. [Performance Test Plan](PERFORMANCE_TEST_PLAN.md): volume testing goals and measurements.
6. [UAT Checklist](UAT_CHECKLIST.md): accounting user verification and sign-off.
7. [Go-Live Validation](GO_LIVE_VALIDATION.md): final production readiness and Go / No-Go decision.

## How To Execute Each Phase

Automated validation uses local or CI-safe commands:

```bash
ruff check .
ruff format --check .
pytest
```

Docker validation, when required by the delivery workflow, should stay local and should not call Uyumsoft or Odoo unless an explicit smoke test is approved:

```bash
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8000/health
```

Integration and UAT phases should use synthetic fixtures or approved test-environment data. Production validation must follow the gates in [Production Readiness](../PRODUCTION_READINESS.md).

## Relationship With ADRs

The testing package operationalizes the decisions recorded in [Architecture Decision Records](../adr/README.md), especially:

- provider boundaries
- Uyumsoft read-only policy
- provider-independent normalized models
- Odoo resolution behavior
- ETTN idempotency
- Odoo draft-only policy
- production safety gates
- local persistence after external writes

If an ADR changes, review this package and update any affected test expectations in the same PR.

## Relationship With Production Readiness

[Production Readiness](../PRODUCTION_READINESS.md) defines runtime gates, environment separation, logging/redaction expectations, rollback, backups, and go-live controls. This testing package turns those controls into executable validation activities and sign-off checklists.

Do not treat a green automated test run as production approval. Production approval also requires configuration review, UAT approval, rollback readiness, secret management validation, and the final Go / No-Go decision.

# UAT Checklist

Use this checklist with accounting users in an approved UAT or staging environment. Odoo draft vendor bills must remain draft-only; posting remains a finance-controlled action outside the Integration Hub flow.

## Preparation

- [ ] UAT environment identified.
- [ ] Test invoices selected and approved for UAT use.
- [ ] Uyumsoft and Odoo credentials are stored outside the repository.
- [ ] Production gates remain disabled unless this is an approved production validation.
- [ ] Finance users have access to inspect draft vendor bills in Odoo.
- [ ] Technical owner has confirmed logs redact secrets and payloads.

## Invoice Review

For each representative invoice:

- [ ] Supplier name is correct.
- [ ] Supplier tax number is correct.
- [ ] Customer/company information is correct.
- [ ] Invoice number is correct.
- [ ] Invoice date is correct.
- [ ] Currency is correct.
- [ ] Line descriptions are understandable.
- [ ] Quantities are correct.
- [ ] Unit prices are correct.
- [ ] Taxes are correct.
- [ ] Totals match the source invoice.
- [ ] Purchase journal is correct.
- [ ] Odoo record is in draft status.
- [ ] UUID/ETTN is visible or traceable.
- [ ] XML attachment is available when UBL XML download is part of the tested flow.
- [ ] Duplicate prevention was verified by repeating a safe test case.
- [ ] Missing or ambiguous partner/product/tax cases are clearly reviewable.
- [ ] No invoice was posted automatically.

## Exceptions

Record any mismatch:

| Invoice / ETTN | Issue | Expected Value | Actual Value | Owner | Resolution |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## Sign-Off

Finance:

- Name:
- Role:
- Decision: Approved / Not Approved
- Date:
- Notes:

Technical Owner:

- Name:
- Role:
- Decision: Approved / Not Approved
- Date:
- Notes:

Project Owner:

- Name:
- Role:
- Decision: Approved / Not Approved
- Date:
- Notes:

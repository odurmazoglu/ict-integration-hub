# ICT V1 — "Taslak Faturayı Aç" (ir.actions.server, model x_ipp_import_workbench, state code).
# Read-only: opens the Vendor Bill the Hub created for this review. Writes nothing.
if len(records) != 1:
    raise UserError("Bu işlem tek bir kayıt için çalışır.")
if not records.x_studio_vendor_bill:
    raise UserError("Bu inceleme için oluşturulmuş taslak fatura yok.")
action = {
    "type": "ir.actions.act_window",
    "res_model": "account.move",
    "res_id": records.x_studio_vendor_bill.id,
    "views": [[False, "form"]],
    "target": "current",
}

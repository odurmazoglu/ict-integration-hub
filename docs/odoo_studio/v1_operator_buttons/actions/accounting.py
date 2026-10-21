# ICT V1 — "Muhasebeyi Kaydet" (ir.actions.server, model x_ipp_import_workbench, state code).
# Validates the step's inputs, refuses double submission, then queues one Hub request.
# It copies values only; the Hub re-validates accounts (type, company, allowlist) and decides.
if len(records) != 1:
    raise UserError("Bu işlem tek bir kayıt için çalışır.")
record = records
if record.x_studio_ipp_req_ready:
    raise UserError("Bu kayıt için gönderilmiş bir istek var. Sonucu bekleyin.")
if record.x_studio_ipp_next_action != "Muhasebe İşlemi Seçilmeli":
    raise UserError("Bu adım artık geçerli değil. Sayfayı yenileyin.")
if not record.x_studio_review_version:
    raise UserError("Kayıt henüz Hub ile eşitlenmedi. Biraz sonra tekrar deneyin.")
missing = []
treatment = record.x_studio_ipp_req_treatment
if not treatment:
    missing.append("Muhasebe İşlemi")
elif treatment == "Gider Hesabı":
    if not record.x_studio_ipp_req_expense_account:
        missing.append("Gider Hesabı")
    if not record.x_studio_ipp_req_expense_category_code:
        missing.append("Gider Kategorisi")
elif treatment == "Sabit Kıymet / Demirbaş":
    if not record.x_studio_ipp_req_asset_account:
        missing.append("Demirbaş Hesabı")
    if not record.x_studio_ipp_req_depreciation_model:
        missing.append("Amortisman Modeli")
if missing:
    raise UserError("Eksik bilgi: " + ", ".join(missing) + ".")
if (
    treatment == "Sabit Kıymet / Demirbaş"
    and record.x_studio_ipp_req_asset_account not in record.x_studio_ipp_eligible_asset_account_ids
):
    raise UserError("Seçilen demirbaş hesabı izin verilen hesaplar arasında değil.")
record.write(
    {
        "x_studio_ipp_req_action": "Muhasebe İşlemi",
        "x_studio_ipp_req_version": record.x_studio_review_version,
        "x_studio_ipp_req_requested_by": env.user.id,
        "x_studio_ipp_req_requested_at": datetime.datetime.now(),
        "x_studio_ipp_req_result": False,
        "x_studio_ipp_req_message": False,
        "x_studio_ipp_req_ready": True,
    }
)

# ICT V1 — "Satın Alma Amacını Kaydet" (ir.actions.server, model x_ipp_import_workbench, state code).
# Validates the step's inputs, refuses double submission, then queues one Hub request.
# It copies values only; the Hub re-validates everything (incl. the product-invoice purpose gate).
if len(records) != 1:
    raise UserError("Bu işlem tek bir kayıt için çalışır.")
record = records
if record.x_studio_ipp_req_ready:
    raise UserError("Bu kayıt için gönderilmiş bir istek var. Sonucu bekleyin.")
if record.x_studio_ipp_next_action not in ("Satın Alma Amacı Seçilmeli", "Karar Verilmeli"):
    raise UserError("Bu adım artık geçerli değil. Sayfayı yenileyin.")
if not record.x_studio_review_version:
    raise UserError("Kayıt henüz Hub ile eşitlenmedi. Biraz sonra tekrar deneyin.")
if not record.x_studio_ipp_req_purpose:
    raise UserError("Eksik bilgi: Satın Alma Amacı.")
record.write(
    {
        "x_studio_ipp_req_action": "Satın Alma Amacı",
        "x_studio_ipp_req_version": record.x_studio_review_version,
        "x_studio_ipp_req_requested_by": env.user.id,
        "x_studio_ipp_req_requested_at": datetime.datetime.now(),
        "x_studio_ipp_req_result": False,
        "x_studio_ipp_req_message": False,
        "x_studio_ipp_req_ready": True,
    }
)

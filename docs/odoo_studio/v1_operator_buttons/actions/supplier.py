# ICT V1 — "Tedarikçiyi Kaydet" (ir.actions.server, model x_ipp_import_workbench, state code).
# Validates the step's inputs, refuses double submission, then queues one Hub request.
# It copies values only; the Hub re-validates everything and decides.
if len(records) != 1:
    raise UserError("Bu işlem tek bir kayıt için çalışır.")
record = records
if record.x_studio_ipp_req_ready:
    raise UserError("Bu kayıt için gönderilmiş bir istek var. Sonucu bekleyin.")
if record.x_studio_ipp_next_action != "Tedarikçi Doğrulanmalı":
    raise UserError("Bu adım artık geçerli değil. Sayfayı yenileyin.")
if not record.x_studio_review_version:
    raise UserError("Kayıt henüz Hub ile eşitlenmedi. Biraz sonra tekrar deneyin.")
missing = []
if not record.x_studio_ipp_req_supplier_mode:
    missing.append("Tedarikçi İşlemi")
elif record.x_studio_ipp_req_supplier_mode == "Mevcut Tedarikçiyi Seç" and not record.x_studio_ipp_req_partner:
    missing.append("Mevcut Tedarikçi")
if missing:
    raise UserError("Eksik bilgi: " + ", ".join(missing) + ".")
record.write(
    {
        "x_studio_ipp_req_action": "Tedarikçi Çözümü",
        "x_studio_ipp_req_version": record.x_studio_review_version,
        "x_studio_ipp_req_requested_by": env.user.id,
        "x_studio_ipp_req_requested_at": datetime.datetime.now(),
        "x_studio_ipp_req_result": False,
        "x_studio_ipp_req_message": False,
        "x_studio_ipp_req_ready": True,
    }
)

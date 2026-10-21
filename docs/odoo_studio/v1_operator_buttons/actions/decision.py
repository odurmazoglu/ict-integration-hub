# ICT V1 — "Kararı Gönder" (ir.actions.server, model x_ipp_import_workbench, state code).
# Validates the decision inputs, refuses double submission, then queues one Hub "Karar" request.
# It never creates a Vendor Bill: execution is the separate "Taslak Alış Faturası Oluştur" step.
if len(records) != 1:
    raise UserError("Bu işlem tek bir kayıt için çalışır.")
record = records
if record.x_studio_ipp_req_ready:
    raise UserError("Bu kayıt için gönderilmiş bir istek var. Sonucu bekleyin.")
if record.x_studio_ipp_next_action != "Karar Verilmeli":
    raise UserError("Bu adım artık geçerli değil. Sayfayı yenileyin.")
if not record.x_studio_review_version:
    raise UserError("Kayıt henüz Hub ile eşitlenmedi. Biraz sonra tekrar deneyin.")
missing = []
if not record.x_studio_decision:
    missing.append("Karar")
elif record.x_studio_decision == "Submit Decision" and not record.x_studio_selected_workflow:
    missing.append("İş Akışı")
elif record.x_studio_decision == "Dismiss" and not (record.x_studio_decision_comment or "").strip():
    missing.append("Karar Notu (reddetme gerekçesi)")
if missing:
    raise UserError("Eksik bilgi: " + ", ".join(missing) + ".")
record.write(
    {
        "x_studio_ipp_req_action": "Karar",
        "x_studio_ipp_req_version": record.x_studio_review_version,
        "x_studio_ipp_req_requested_by": env.user.id,
        "x_studio_ipp_req_requested_at": datetime.datetime.now(),
        "x_studio_ipp_req_result": False,
        "x_studio_ipp_req_message": False,
        "x_studio_ipp_req_ready": True,
    }
)

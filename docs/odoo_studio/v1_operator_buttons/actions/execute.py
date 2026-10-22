# ICT V1 — "Taslak Alış Faturası Oluştur" (ir.actions.server, model x_ipp_import_workbench, state code).
# Queues one Hub "Fatura Oluştur" request for the already accepted decision. The Hub issues
# the single-use authorization, creates a DRAFT bill only (never posts) and is idempotent.
if len(records) != 1:
    raise UserError("Bu işlem tek bir kayıt için çalışır.")
record = records
if record.x_studio_ipp_req_ready:
    raise UserError("Bu kayıt için gönderilmiş bir istek var. Sonucu bekleyin.")
if record.x_studio_ipp_next_action != "Fatura Oluşturulmalı":
    raise UserError("Bu adım artık geçerli değil. Sayfayı yenileyin.")
if record.x_studio_review_status != "Decision Submitted":
    raise UserError("Önce karar kabul edilmelidir.")
if record.x_studio_vendor_bill:
    raise UserError("Bu inceleme için taslak fatura zaten oluşturuldu.")
if not record.x_studio_review_version:
    raise UserError("Kayıt henüz Hub ile eşitlenmedi. Biraz sonra tekrar deneyin.")
record.write(
    {
        "x_studio_ipp_req_action": "Fatura Oluştur",
        "x_studio_ipp_req_version": record.x_studio_review_version,
        "x_studio_ipp_req_requested_by": env.user.id,
        "x_studio_ipp_req_requested_at": datetime.datetime.now(),
        "x_studio_ipp_req_result": False,
        "x_studio_ipp_req_message": False,
        "x_studio_ipp_req_ready": True,
    }
)

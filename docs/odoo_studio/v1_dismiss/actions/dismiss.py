# ICT V1 — "Muhasebeleştirme Dışı Bırak" (ir.actions.server, model x_ipp_import_workbench, state code).
# Queues one Hub "Karar" request with Karar = Dismiss for a PENDING review, at any open step:
# supplier / product matching, purpose or accounting need not be completed first.
# It never creates, changes or posts a Vendor Bill, and it is not a legal e-Fatura rejection
# or cancellation: nothing is sent to Uyumsoft, the supplier or GİB. The Hub re-checks
# everything (pending status, exact version, permission, reason) and refuses otherwise.
if len(records) != 1:
    raise UserError("Bu işlem tek bir kayıt için çalışır.")
record = records
if record.x_studio_ipp_req_ready:
    raise UserError("Bu kayıt için gönderilmiş bir istek var. Sonucu bekleyin.")
if (
    record.x_studio_review_status != "Pending Review"
    or record.x_studio_vendor_bill
    or record.x_studio_ipp_next_action in (False, "Tamamlandı", "Fatura Oluşturulmalı")
):
    raise UserError(
        "Bu kayıt muhasebeleştirme dışı bırakılamaz: karar kabul edilmiş, fatura oluşturulmuş veya "
        "inceleme kapanmış. Sayfayı yenileyin."
    )
if not record.x_studio_review_version:
    raise UserError("Kayıt henüz Hub ile eşitlenmedi. Biraz sonra tekrar deneyin.")
reason = (record.x_studio_decision_comment or "").strip()
if len(reason) < 10:
    raise UserError(
        "Gerekçe zorunludur: 'Karar Notu / Dışı Bırakma Gerekçesi' alanına faturanın neden Hub'da "
        "işlenmeyeceğini en az 10 karakterle yazın."
    )
record.write(
    {
        "x_studio_decision": "Dismiss",
        "x_studio_decision_comment": reason,
        "x_studio_ipp_req_action": "Karar",
        "x_studio_ipp_req_version": record.x_studio_review_version,
        "x_studio_ipp_req_requested_by": env.user.id,
        "x_studio_ipp_req_requested_at": datetime.datetime.now(),
        "x_studio_ipp_req_result": False,
        "x_studio_ipp_req_message": False,
        "x_studio_ipp_req_ready": True,
    }
)

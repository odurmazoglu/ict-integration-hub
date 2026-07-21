class InvoiceDomainError(Exception):
    safe_message = "Invoice domain processing failed."

    def __init__(self, message: str | None = None, *, field_path: str | None = None) -> None:
        super().__init__(message or self.safe_message)
        self.safe_message = message or self.safe_message
        self.field_path = field_path


class InvalidInvoiceXmlError(InvoiceDomainError):
    safe_message = "Invoice XML is invalid."


class UnsupportedInvoiceXmlError(InvoiceDomainError):
    safe_message = "XML document is not a supported UBL invoice."


class MissingMandatoryInvoiceFieldError(InvoiceDomainError):
    safe_message = "Mandatory invoice identifier is missing."

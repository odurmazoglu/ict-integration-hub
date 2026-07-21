class TaxMappingError(Exception):
    safe_message = "Tax mapping failed."

    def __init__(self, safe_message: str | None = None) -> None:
        super().__init__(safe_message or self.safe_message)
        self.safe_message = safe_message or self.safe_message

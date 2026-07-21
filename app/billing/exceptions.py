class VendorBillBuildError(Exception):
    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("Vendor bill cannot be built.")
        self.errors = errors
        self.safe_message = "; ".join(errors)

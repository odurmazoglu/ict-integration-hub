class ErpRepositoryError(Exception):
    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


class ErpRepositoryTimeoutError(ErpRepositoryError):
    pass


class ErpRepositoryResponseError(ErpRepositoryError):
    pass


class ErpReadonlyViolationError(ErpRepositoryError):
    pass

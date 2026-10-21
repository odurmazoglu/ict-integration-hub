class ConnectorError(Exception):
    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


class ConnectorTimeoutError(ConnectorError):
    pass


class ConnectorConnectTimeoutError(ConnectorTimeoutError):
    """The connection could not be established in time: the request was never sent."""


class ConnectorConnectionError(ConnectorError):
    """The connection failed before a response (refused, reset, DNS); outcome of a write is unknown."""


class ConnectorAuthenticationError(ConnectorError):
    pass


class ConnectorAuthorizationError(ConnectorError):
    pass


class ConnectorValidationError(ConnectorError):
    pass

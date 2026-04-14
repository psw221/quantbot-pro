class QuantBotError(Exception):
    """Base application error."""


class ConfigurationError(QuantBotError):
    """Raised when configuration is invalid or incomplete."""


class WriterQueueError(QuantBotError):
    """Raised for writer queue failures."""


class WriterQueueDegradedError(WriterQueueError):
    """Raised when the queue is degraded after repeated failures."""


class AuthenticationError(QuantBotError):
    """Raised when token issuance or validation fails."""


class BrokerApiError(QuantBotError):
    """Raised when the broker API returns an error response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

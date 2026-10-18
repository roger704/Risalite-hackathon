"""Stable, transport-independent errors for ingestion and dataset queries."""


class ConnectorError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class ConfigError(ConnectorError):
    def __init__(self, message: str, code: str = "CONFIG_INVALID"):
        super().__init__(code, message)


class SchemaError(ConnectorError):
    def __init__(self, message: str, code: str = "SCHEMA_CHANGED"):
        super().__init__(code, message)


class ValidationError(ConnectorError):
    def __init__(self, message: str, code: str = "VALIDATION_FAILED"):
        super().__init__(code, message)


class SourceUnavailableError(ConnectorError):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__("SOURCE_UNAVAILABLE", message, retryable)


class NotFoundError(ConnectorError):
    def __init__(self, message: str, code: str = "NOT_FOUND"):
        super().__init__(code, message)


class IncompatibleSnapshotError(ConnectorError):
    def __init__(self, message: str):
        super().__init__("SNAPSHOT_INCOMPATIBLE", message)

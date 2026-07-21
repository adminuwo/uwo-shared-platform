"""Stable fail-closed billing error contracts."""


class BillingServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AuthorizationDenied(BillingServiceError):
    pass


class ResourceNotFound(BillingServiceError):
    pass


class Conflict(BillingServiceError):
    pass


class InvalidRequest(BillingServiceError):
    pass


class PaymentRequired(BillingServiceError):
    pass


class RepositoryIntegrityError(RuntimeError):
    """Unexpected persistence failure never exposed to callers."""

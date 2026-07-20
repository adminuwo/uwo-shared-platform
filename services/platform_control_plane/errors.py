"""Fail-closed control-plane error contracts."""


class ControlPlaneError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AuthorizationDenied(ControlPlaneError):
    pass


class ResourceNotFound(ControlPlaneError):
    pass


class Conflict(ControlPlaneError):
    pass


class StaleVersion(Conflict):
    pass


class InvalidRequest(ControlPlaneError):
    pass

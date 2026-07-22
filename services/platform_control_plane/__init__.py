"""Identity, tenancy, roles, and entitlements control-plane service."""

__all__ = ["PlatformControlPlane"]


def __getattr__(name: str):
    """Load the service lazily so shared authorization modules remain acyclic."""
    if name == "PlatformControlPlane":
        from .service import PlatformControlPlane
        return PlatformControlPlane
    raise AttributeError(name)

"""HTTP API and static dashboard for SelfHeal."""

from .app import create_app
from .settings import ApiSettings

__all__ = ["ApiSettings", "create_app"]

"""Errors surfaced by optional external services."""


class IntegrationUnavailable(RuntimeError):
    """A service is not configured or cannot be used right now."""

    def __init__(self, service: str, reason: str):
        self.service = service
        self.reason = reason
        super().__init__(f"{service}: {reason}")

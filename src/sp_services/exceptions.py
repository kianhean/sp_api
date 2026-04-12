"""Exceptions raised by the SP Services client."""


class SpServicesError(Exception):
    """Base exception for this package."""


class AuthenticationError(SpServicesError):
    """Raised when credentials or OTP verification fail."""


class SessionExpiredError(AuthenticationError):
    """Raised when an authenticated token is no longer valid."""


class ApiError(SpServicesError):
    """Raised when the upstream SP Services API returns an unexpected response."""

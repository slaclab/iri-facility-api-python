"""Per-request URL context derived from forwarding headers. (e.g. for Kong or other API gateways)"""
from contextvars import ContextVar

from fastapi import Request

from . import config

_api_url_base: ContextVar[str | None] = ContextVar("_api_url_base", default=None)

# Auth headers injected by the s3df-authnz Traefik middleware (ForwardAuth).
_AUTH_HEADER_NAMES = (
    "x-auth-request-primary-gid",
    "x-auth-request-gids",
    'x-auth-request-uid'
)

_auth_headers: ContextVar[dict[str, str]] = ContextVar("_auth_headers", default={})


def set_api_url_base(request: Request) -> None:
    """Set the per-request API URL base from forwarding headers."""
    host = (request.headers.get("x-forwarded-host") or
            request.headers.get("host", "")).split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto") or
             request.url.scheme).split(",")[0].strip()
    prefix = (request.headers.get("x-forwarded-prefix")
              or request.headers.get("x-script-name")
              or "").rstrip("/")
    api_url = config.API_URL.strip("/")
    if host:
        _api_url_base.set(f"{proto}://{host}{prefix}/{api_url}")

def set_auth_headers(request: Request) -> None:
    """Capture the authnz-injected headers from the current request into context."""
    headers = {name: request.headers[name] for name in _AUTH_HEADER_NAMES if name in request.headers}
    _auth_headers.set(headers)


def get_auth_headers() -> dict[str, str]:
    """Return the authnz-injected headers for the current request."""
    return _auth_headers.get()

def get_url_prefix() -> str:
    """Return the per-request API URL base, or fall back to static config."""
    value = _api_url_base.get()
    if value:
        return value
    return f"{config.API_URL_ROOT}{config.API_PREFIX}{config.API_URL}"

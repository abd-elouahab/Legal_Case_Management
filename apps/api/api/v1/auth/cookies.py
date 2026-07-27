"""Refresh-token cookie handling.

The refresh token is delivered to browsers as an httpOnly cookie rather than
being kept in JavaScript-reachable storage:

* **httpOnly** — script injected by an XSS cannot read the long-lived token.
* **SameSite** (``strict`` by default) — the browser will not attach the cookie
  to cross-site requests, which is what makes this strategy CSRF-safe. Regular
  API calls authenticate with an ``Authorization: Bearer`` header, never with a
  cookie, so a forged cross-site request carries no usable credential.
* **Secure** — required in production so the cookie is HTTPS-only.

``Path`` is ``/`` (not scoped to the auth endpoints) so the Next.js server can
read the cookie during route protection and decide between the login page and
the app before rendering.
"""

from __future__ import annotations

from fastapi import Response

from core.config import settings


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Attach the refresh token to the response as an httpOnly cookie."""
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=int(settings.refresh_token_ttl.total_seconds()),
        httponly=True,
        secure=settings.REFRESH_COOKIE_SECURE,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
        domain=settings.REFRESH_COOKIE_DOMAIN,
        path="/",
    )


def clear_refresh_cookie(response: Response) -> None:
    """Remove the refresh cookie.

    The attributes must match those used when setting it, or the browser will
    keep the original cookie.
    """
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        httponly=True,
        secure=settings.REFRESH_COOKIE_SECURE,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
        domain=settings.REFRESH_COOKIE_DOMAIN,
        path="/",
    )

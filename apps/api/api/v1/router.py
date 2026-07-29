"""Aggregate router for API version 1.

Feature routers are registered here as they are implemented.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.v1.auth.router import router as auth_router
from api.v1.authorization.router import router as authorization_router
from api.v1.users.router import router as users_router

api_router = APIRouter()

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(authorization_router, prefix="/authorization", tags=["authorization"])
api_router.include_router(users_router, prefix="/users", tags=["users"])

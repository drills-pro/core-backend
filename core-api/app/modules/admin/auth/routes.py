"""The admin console's sign-in, under /v1/admin/auth. Two separate ways in,
one file each:

- ./zitadel.py   OIDC against Zitadel: /login, /callback, /redeem,
                 /logout, /me
- ./password.py  core-api's own accounts and their CLI: /password

Both answer the console in the same shape, so it keeps one kind of
session whichever way someone came in.
"""
from fastapi import APIRouter

from app.modules.admin.auth import password, zitadel

router = APIRouter(prefix="/v1/admin/auth", tags=["admin"])
router.include_router(zitadel.router)
router.include_router(password.router)

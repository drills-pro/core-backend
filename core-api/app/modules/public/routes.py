"""Unauthenticated public discovery (improvement/phase2/08-collaboration-visibility.md).

FR5/FR8: no tenant JWT, no X-Tenant-Id — this is the one place Phase 2
deliberately bypasses tenant RLS, scoped tightly to visibility='public'
rows via SECURITY DEFINER functions (migrations/0021), never a blanket
bypass. A private dataset/model inside a public project is listed with no
name — "exists" is visible, contents are not, unless the owner opted in.
"""
import uuid

from fastapi import APIRouter, Query

from app.core.db import get_pool

router = APIRouter(prefix="/v1/public", tags=["public"])


@router.get("/projects")
async def list_public_projects(limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM list_public_projects($1, $2)", limit, offset)
    return [dict(r) for r in rows]


@router.get("/projects/{project_id}/datasets")
async def list_public_project_datasets(project_id: uuid.UUID):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM list_public_project_datasets($1)", project_id)
    return [dict(r) for r in rows]


@router.get("/projects/{project_id}/models")
async def list_public_project_models(project_id: uuid.UUID):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM list_public_project_models($1)", project_id)
    return [dict(r) for r in rows]

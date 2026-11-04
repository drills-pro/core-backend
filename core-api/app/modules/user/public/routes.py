"""Unauthenticated public discovery (improvement/phase2/08-collaboration-visibility.md).

FR5/FR8: no tenant JWT, no X-Tenant-Id — this is the one place Phase 2
deliberately bypasses tenant RLS, scoped tightly to visibility='public'
rows via SECURITY DEFINER functions (list_public_*() in the init migration), never a blanket
bypass. A private dataset/model inside a public project is listed with no
name — "exists" is visible, contents are not, unless the owner opted in.
"""
import uuid

from fastapi import APIRouter, Depends, Query

from app.core.authz import public
from app.modules.user.public import service
from app.modules.user.public.schemas import PublicDataset, PublicModel, PublicProject

router = APIRouter(prefix="/v1/public", tags=["public"], dependencies=[Depends(public)])


@router.get("/projects", response_model=list[PublicProject])
async def list_public_projects(limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
    return await service.list_projects(limit, offset)


@router.get("/projects/{project_id}/datasets", response_model=list[PublicDataset])
async def list_public_project_datasets(project_id: uuid.UUID):
    return await service.list_datasets(project_id)


@router.get("/projects/{project_id}/models", response_model=list[PublicModel])
async def list_public_project_models(project_id: uuid.UUID):
    return await service.list_models(project_id)

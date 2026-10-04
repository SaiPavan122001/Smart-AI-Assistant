from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AuthDep, ContainerDep
from app.permissions.policy import PermissionPolicy
from app.state.repository import PermissionDecisionRecord
from app.utils.errors import AgentError, ErrorCode

router = APIRouter(prefix="/permissions", tags=["permissions"], dependencies=[AuthDep])


@router.get("", response_model=PermissionPolicy)
async def get_permissions(c: ContainerDep) -> PermissionPolicy:
    return c.engine.policy


@router.put("", response_model=PermissionPolicy)
async def update_permissions(body: PermissionPolicy, c: ContainerDep) -> PermissionPolicy:
    """Replace the policy. CRITICAL risk can never be auto-allowed (normalised server-side)."""
    unknown = {t for t in body.tool_overrides if t not in c.registry}
    unknown |= {op.tool for op in body.trusted_operations if op.tool not in c.registry}
    if unknown:
        raise AgentError(
            f"Unknown tool(s): {', '.join(sorted(unknown))}", code=ErrorCode.INVALID_INPUT
        )
    updated = c.engine.update_policy(body)
    await c.repo.save_policy(updated.model_dump(mode="json"))
    return updated


@router.get("/decisions", response_model=list[PermissionDecisionRecord])
async def decisions(
    c: ContainerDep, task_id: Annotated[str, Query(min_length=1)]
) -> list[PermissionDecisionRecord]:
    return await c.repo.list_permission_decisions(task_id)

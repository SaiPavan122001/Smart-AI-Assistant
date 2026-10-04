from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AuthDep, ContainerDep
from app.api.schemas import ApproveRequest, DenyRequest
from app.models.approval import ApprovalDecision, ApprovalRequest
from app.utils.errors import NotFoundError

router = APIRouter(prefix="/approvals", tags=["approvals"], dependencies=[AuthDep])


@router.get("", response_model=list[ApprovalRequest])
async def list_approvals(
    c: ContainerDep, pending: Annotated[bool, Query()] = True
) -> list[ApprovalRequest]:
    if pending:
        return c.approvals.pending()
    return await c.repo.list_approvals()


@router.get("/{approval_id}", response_model=ApprovalRequest)
async def get_approval(approval_id: str, c: ContainerDep) -> ApprovalRequest:
    found = c.approvals.get(approval_id) or await c.repo.get_approval(approval_id)
    if found is None:
        raise NotFoundError(f"Approval {approval_id} not found")
    return found


@router.post("/{approval_id}/approve", response_model=ApprovalRequest)
async def approve(approval_id: str, body: ApproveRequest, c: ContainerDep) -> ApprovalRequest:
    return await c.approvals.resolve(
        approval_id, ApprovalDecision(approved=True, scope=body.scope, note=body.note)
    )


@router.post("/{approval_id}/deny", response_model=ApprovalRequest)
async def deny(approval_id: str, body: DenyRequest, c: ContainerDep) -> ApprovalRequest:
    return await c.approvals.resolve(approval_id, ApprovalDecision(approved=False, note=body.note))

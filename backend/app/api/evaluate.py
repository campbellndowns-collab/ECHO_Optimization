from __future__ import annotations

from fastapi import APIRouter

from backend.app.models.evaluate import FixedEvaluateRequest, FixedEvaluateResponse
from backend.app.services.evaluate import evaluate_fixed

router = APIRouter(tags=["evaluate"])


@router.post("/evaluate", response_model=FixedEvaluateResponse)
def post_evaluate(body: FixedEvaluateRequest) -> FixedEvaluateResponse:
    """Synchronous fixed-configuration aircraft evaluation."""
    return evaluate_fixed(body)

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any

from ..core.eval_runner import run_eval as do_run_eval
from ..db import get_session
from ..models import Eval, EvalRun
from ..schemas import EvalOut, EvalRunOut

router = APIRouter(prefix="/api/evals", tags=["evals"])


class EvalIn(BaseModel):
    slug: str
    name: str
    description: str = ""
    target_kind: str           # agent|workflow
    target_slug: str
    dataset: list[dict[str, Any]]
    metric: str                # judge_llm|assert_contains|cmd_returns_zero|tool_sequence_match
    metric_args: dict[str, Any] = {}


@router.get("/_resettable")
def list_resettable_evals():
    from ..seed import SEED_EVALS
    return {x["slug"] for x in SEED_EVALS}


@router.get("", response_model=list[EvalOut])
def list_evals(s: Session = Depends(get_session)):
    return s.query(Eval).order_by(Eval.name).all()


@router.get("/{slug}", response_model=EvalOut)
def get_eval(slug: str, s: Session = Depends(get_session)):
    e = s.query(Eval).filter(Eval.slug == slug).first()
    if not e:
        raise HTTPException(404, "not found")
    return e


@router.post("", response_model=EvalOut)
def create_eval(body: EvalIn, s: Session = Depends(get_session)):
    if s.query(Eval).filter(Eval.slug == body.slug).first():
        raise HTTPException(409, "slug already exists")
    e = Eval(**body.model_dump())
    s.add(e); s.commit(); s.refresh(e)
    return e


@router.put("/{slug}", response_model=EvalOut)
def update_eval(slug: str, body: EvalIn, s: Session = Depends(get_session)):
    e = s.query(Eval).filter(Eval.slug == slug).first()
    if not e:
        raise HTTPException(404, "not found")
    for k, v in body.model_dump().items():
        setattr(e, k, v)
    s.commit(); s.refresh(e)
    return e


@router.delete("/{slug}")
def delete_eval(slug: str, s: Session = Depends(get_session)):
    e = s.query(Eval).filter(Eval.slug == slug).first()
    if not e:
        raise HTTPException(404, "not found")
    s.delete(e); s.commit()
    return {"deleted": slug}


@router.post("/{slug}/reset", response_model=EvalOut)
def reset_eval(slug: str, s: Session = Depends(get_session)):
    """Restore an eval to its seed-list defaults."""
    from ..seed import SEED_EVALS
    spec = next((x for x in SEED_EVALS if x["slug"] == slug), None)
    if spec is None:
        raise HTTPException(400, "no seed defaults exist for this slug")
    e = s.query(Eval).filter(Eval.slug == slug).first()
    if e is None:
        e = Eval(**spec)
        s.add(e)
    else:
        for k, v in spec.items():
            setattr(e, k, v)
    s.commit(); s.refresh(e)
    return e


@router.post("/{slug}/run")
async def run_eval_ep(slug: str, s: Session = Depends(get_session)):
    e = s.query(Eval).filter(Eval.slug == slug).first()
    if not e:
        raise HTTPException(404, "not found")
    result = await do_run_eval(slug)
    return result


@router.get("/runs/{eval_run_id}", response_model=EvalRunOut)
def get_eval_run(eval_run_id: str, s: Session = Depends(get_session)):
    r = s.query(EvalRun).filter(EvalRun.id == eval_run_id).first()
    if not r:
        raise HTTPException(404, "not found")
    return r

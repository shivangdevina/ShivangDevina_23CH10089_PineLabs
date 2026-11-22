"""Explicit, opt-in, read-only synthetic preview. Never a live-database fallback."""

from copy import deepcopy
from functools import lru_cache
import os

from fastapi import APIRouter, Depends, HTTPException

from .analytics import analyse, scenario_from_analysis
from .contracts import AnalysisRequest, PreviewQuestionRequest, PreviewScenarioRequest, ResourceNotFound
from .data import DatasetConfig, generate
from .database import load_environment
from .planner import plan_question
from .presentation import present
from .preview import snapshot_from_generated
from .queries import DAILY_SQL

router = APIRouter(prefix="/preview", tags=["Explicit synthetic preview"])


def preview_enabled() -> bool:
    load_environment()
    return os.environ.get("ENABLE_SYNTHETIC_PREVIEW", "false").lower() in {"true", "1", "yes"}


def require_preview():
    if not preview_enabled():
        raise HTTPException(404, "Synthetic preview is disabled. Start locally with serve --preview to enable it.")


@lru_cache(maxsize=2)
def fixture_snapshot(name):
    scenario = "healthy" if name == "healthy" else "incidents"
    return snapshot_from_generated(generate(DatasetConfig(scenario=scenario)))


def preview_snapshot(fixture):
    snapshot = deepcopy(fixture_snapshot("healthy" if fixture == "healthy" else "incidents"))
    if fixture == "data_quality_failure":
        snapshot.daily.append(deepcopy(snapshot.daily[0]))
    return snapshot


def make_preview_report(fixture, request):
    snapshot = preview_snapshot(fixture)
    if str(request.dataset_id) != snapshot.version["id"]:
        raise ResourceNotFound("Preview dataset does not match the selected fixture.")
    snapshot.query_evidence.append({"template_id": "daily-matched-cohorts-v1", "sql": DAILY_SQL,
        "parameters": {"dataset_id": str(request.dataset_id), "banks": request.banks or None,
                       "methods": request.payment_methods or None},
        "execution": "NOT_EXECUTED", "note": "Illustrative query template; preview calculations use generated in-memory aggregates."})
    return analyse(snapshot, request)


@router.get("/datasets", dependencies=[Depends(require_preview)])
def datasets():
    items = []
    for fixture, label in [("incidents", "Incident snapshot"), ("healthy", "Healthy control"),
                           ("data_quality_failure", "Duplicate aggregate · exception demo")]:
        version = fixture_snapshot("healthy" if fixture == "healthy" else "incidents").version
        items.append({"id": version["id"], "fixture": fixture, "label": label,
                      "current_start": version["current_start"], "window_end": version["window_end"],
                      "is_synthetic": True, "saved": False})
    return {"datasets": items}


@router.post("/questions", dependencies=[Depends(require_preview)])
def question(body: PreviewQuestionRequest):
    compiled, planning = plan_question(body, force_guided=True)
    if compiled is None:
        return planning
    report = present(make_preview_report(body.fixture, compiled), body.question, planning)
    return {"id": None, "status": "COMPLETED", "saved": False, "analysis": report}


@router.post("/scenarios", dependencies=[Depends(require_preview)])
def scenario(body: PreviewScenarioRequest):
    report = make_preview_report(body.fixture, body.analysis_request)
    result = scenario_from_analysis(report, body.scenario)
    return {"id": None, "saved": False, "scenario": result}

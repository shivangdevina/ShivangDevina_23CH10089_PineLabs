"""Public API contracts and versioned, reproducible analytics settings."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CALCULATION_VERSION = "matched-retry-v1.0.0"
Bank = Literal["ATLAS", "BREEZE", "CEDAR", "DELTA"]
Method = Literal["UPI", "CARD"]


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: UUID
    banks: list[Bank] = Field(default_factory=list, max_length=4)
    payment_methods: list[Method] = Field(default_factory=list, max_length=2)
    intervention_cost_paise: int = Field(default=0, ge=0, le=1_000_000_000, strict=True)


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_id: str = Field(pattern=r"^(ATLAS|BREEZE|CEDAR|DELTA)__(UPI|CARD)$")
    recovery_fraction: Decimal = Field(default=Decimal("0.5"), ge=0, le=1, allow_inf_nan=False)
    intervention_cost_paise: int = Field(default=0, ge=0, le=1_000_000_000, strict=True)


class QuestionRequest(AnalysisRequest):
    question: str = Field(min_length=3, max_length=1000)


class PreviewQuestionRequest(QuestionRequest):
    fixture: Literal["incidents", "healthy", "data_quality_failure"] = "incidents"


class PreviewScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_request: AnalysisRequest
    fixture: Literal["incidents", "healthy"] = "incidents"
    scenario: ScenarioRequest


@dataclass(frozen=True)
class EngineSettings:
    bootstrap_samples: int = 800
    seed: int = 20261122
    min_current_orders: int = 100
    min_history_orders: int = 500
    min_stratum_history: int = 10
    min_history_days: int = 28
    min_current_days: int = 7
    family_error_rate: float = 0.05


@dataclass
class Snapshot:
    version: dict
    daily: list[dict]
    diagnostics: dict
    query_evidence: list[dict]
    source: str = "supabase"


class AnalysisBlocked(ValueError):
    """Data is insufficient or inconsistent; do not emit a numerical conclusion."""


class ResourceNotFound(ValueError):
    pass


class BackendUnavailable(RuntimeError):
    pass

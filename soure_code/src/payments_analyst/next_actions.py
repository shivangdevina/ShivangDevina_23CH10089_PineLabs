"""Separate SELECT-only action research and deterministic, assumption-explicit ranking."""

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field
from fastapi.encoders import jsonable_encoder

from .contracts import AnalysisBlocked, AnalysisRequest, BackendUnavailable
from .finance import RetryEconomics, simulate
from .research import _current_bank_method_scope, research_transaction

ACTION_VERSION = "costed-retry-actions-v1"
CODES = ("NETWORK_ERROR", "PROVIDER_TIMEOUT")
VARIANTS = (("NETWORK", ("NETWORK_ERROR",), "Retry network failures"),
            ("TIMEOUT", ("PROVIDER_TIMEOUT",), "Retry provider timeouts"),
            ("BOTH", CODES, "Retry both eligible failure types"))
ECONOMICS = {"eligible_orders": "eligible_orders", "eligible_payment_value_paise": "eligible_payment_value_paise",
             "merchant_fee_if_all_recovered_paise": "retry_fee_paise",
             "processing_cost_if_all_succeed_paise": "retry_success_cost_paise",
             "processing_cost_if_all_fail_paise": "retry_failure_cost_paise",
             "loss_provision_if_all_recovered_paise": "retry_loss_provision_paise"}


class NextActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_fraction: Decimal = Field(default=Decimal("0.5"), ge=0, le=1, allow_inf_nan=False)
    intervention_cost_paise: int = Field(default=0, ge=0, le=1_000_000_000, strict=True)


class ActionSimulationRequest(NextActionRequest):
    action_id: str = Field(pattern=r"^(ATLAS|BREEZE|CEDAR|DELTA)__(UPI|CARD)__(NETWORK|TIMEOUT|BOTH)$")


# Trusted pricing query, executed only by the SELECT-only research role. Potential
# future fees are calculated from merchant/cost rules, not failed-order fee receipts.
ACTION_RESEARCH_SQL = """WITH priced AS (
 SELECT f.*, r.rule_count,
   (f.amount_paise*f.fee_bps+5000)/10000+f.fee_fixed_paise AS potential_fee,
   (f.amount_paise*r.success_bps+5000)/10000+r.success_fixed_paise AS potential_success_cost,
   r.failure_fixed_paise AS potential_failure_cost,
   (f.amount_paise*r.expected_loss_bps+5000)/10000 AS potential_loss
 FROM payments.order_facts f JOIN payments.dataset_versions d ON d.id=f.dataset_id
 LEFT JOIN LATERAL (
   SELECT count(*) AS rule_count,min(success_bps) AS success_bps,
     min(success_fixed_paise) AS success_fixed_paise,min(failure_fixed_paise) AS failure_fixed_paise,
     min(expected_loss_bps) AS expected_loss_bps
   FROM payments.cost_rules c WHERE c.dataset_id=f.dataset_id AND c.bank=f.initial_bank
     AND c.payment_method=f.initial_payment_method AND c.currency=f.currency
     AND c.effective_from<=d.as_of AND d.as_of<c.effective_to
 ) r ON true
 WHERE f.dataset_id=%(dataset_id)s AND f.created_at>=d.current_start AND f.created_at<d.window_end
   AND f.is_complete AND f.retry_eligible
   AND (%(banks)s::text[] IS NULL OR f.initial_bank=ANY(%(banks)s::text[]))
   AND (%(methods)s::text[] IS NULL OR f.initial_payment_method=ANY(%(methods)s::text[]))
)
SELECT initial_bank AS bank,initial_payment_method AS payment_method,last_failure_code AS failure_code,
 count(*) AS eligible_orders,count(*) FILTER(WHERE rule_count<>1) AS unpriced_orders,
 sum(amount_paise) AS eligible_payment_value_paise,
 coalesce(sum(potential_fee) FILTER(WHERE rule_count=1),0) AS retry_fee_paise,
 coalesce(sum(potential_success_cost) FILTER(WHERE rule_count=1),0) AS retry_success_cost_paise,
 coalesce(sum(potential_failure_cost) FILTER(WHERE rule_count=1),0) AS retry_failure_cost_paise,
 coalesce(sum(potential_loss) FILTER(WHERE rule_count=1),0) AS retry_loss_provision_paise
FROM priced GROUP BY initial_bank,initial_payment_method,last_failure_code
ORDER BY initial_bank,initial_payment_method,last_failure_code"""


class ReadOnlyActionResearch:
    """One bounded aggregate query; intentionally no persistence or execution tools."""
    __slots__ = ()

    def read(self, request):
        parameters = {"dataset_id": str(request.dataset_id), "banks": request.banks or None,
                      "methods": request.payment_methods or None}
        with research_transaction() as connection:
            rows = connection.execute(ACTION_RESEARCH_SQL, parameters).fetchall()
        if len(rows) > 16:
            raise AnalysisBlocked("Unexpected action research groups. No action was ranked.")
        return jsonable_encoder(rows), {"template_id": "action-costed-failures-v1", "sql": ACTION_RESEARCH_SQL,
                      "parameters": parameters, "execution": "EXECUTED", "row_count": len(rows),
                      "database_role": "payment_analyst_research", "source_window": "current",
                      "executed_at": datetime.now(timezone.utc).isoformat()}


def _integer(value):
    return not isinstance(value, bool) and isinstance(value, (int, Decimal)) and value >= 0 and int(value) == value


def _economics(rows):
    return RetryEconomics(**{field: sum(int(row[column]) for row in rows) for field, column in ECONOMICS.items()})


def action_scenario(action, request):
    if action.get("full_gap_successes") is None:
        raise AnalysisBlocked("This action has no supported, priced historical benchmark.")
    economics = RetryEconomics(**action["economics"])
    result = simulate(economics, action["full_gap_successes"], request.recovery_fraction, request.intervention_cost_paise)
    endpoints = [simulate(economics, gap, request.recovery_fraction, request.intervention_cost_paise)
                 for gap in action["historical_gap_bounds_successes"]]
    bounds = sorted(item["incremental_net_contribution_paise"] for item in endpoints)
    curve = [simulate(economics, action["full_gap_successes"], Decimal(i)/20, request.intervention_cost_paise)
             for i in range(21)]
    return {"action_id": action["action_id"], "segment_id": action["segment_id"],
            "calculation_version": ACTION_VERSION, "result": result, "net_impact_interval_paise": bounds,
            "curve": [{key: point[key] for key in ("recovery_fraction", "incremental_net_contribution_paise", "additional_successful_orders")}
                      for point in curve],
            "caveat": "Conditional historical range at your assumed recovery, not measured treatment-effect confidence or a revenue forecast."}


def rank_actions(report, rows, settings):
    """No opaque weights: rank positive lower net bounds, with deterministic ties."""
    segments = {segment["segment_id"]: segment for segment in report["segments"]}
    groups, seen = {}, set()
    for row in rows:
        key = (row.get("bank"), row.get("payment_method"), row.get("failure_code"))
        if key in seen or key[2] not in CODES:
            raise AnalysisBlocked("Duplicate or unsupported failure groups. No action was ranked.")
        seen.add(key)
        if any(not _integer(row.get(column)) for column in (*ECONOMICS.values(), "unpriced_orders")):
            raise AnalysisBlocked("Invalid action counts or costs. No action was ranked.")
        sid = f"{key[0]}__{key[1]}"
        if sid not in segments:
            raise AnalysisBlocked("Research returned a segment outside the analysis scope.")
        groups.setdefault(sid, []).append(row)
    # Independently reconcile every research fee/cost/count total to canonical data.
    for sid, segment in segments.items():
        priced = groups.get(sid, [])
        if any(row["unpriced_orders"] for row in priced):
            if segment["status"] != "UNPRICED":
                raise AnalysisBlocked("Action pricing differs from the original benchmark.")
            continue
        if asdict(_economics(priced)) != segment["economics"]:
            raise AnalysisBlocked("Action research does not reconcile with the segment's counts and fees.")
    actions = []
    for sid, segment in segments.items():
        segment_rows = groups.get(sid, [])
        total = segment["economics"]["eligible_orders"]
        for suffix, codes, title in VARIANTS:
            selected = [row for row in segment_rows if row["failure_code"] in codes]
            if not selected:
                continue
            economics = _economics(selected)
            candidate = {"action_id": f"{sid}__{suffix}", "segment_id": sid, "bank": segment["bank"],
                "payment_method": segment["payment_method"], "title": f"{segment['bank']} {segment['payment_method']} — {title}",
                "failure_codes": list(codes), "eligible_orders": economics.eligible_orders, "economics": asdict(economics),
                "rank": None, "status": "INSUFFICIENT_EVIDENCE", "full_gap_successes": None,
                "scope": "Current snapshot's unresolved eligible payments, one additional attempt each.",
                "action_steps": [f"Inspect {economics.eligible_orders} eligible failed-payment records and confirm the recorded error codes.",
                                 "Run a controlled retry test; compare recovered fees with successful/failed retry costs and estimated losses.",
                                 "Expand only after the measured net benefit is positive; no live retry is executed by this app."]}
            if any(row["unpriced_orders"] for row in selected):
                candidate.update(status="UNPRICED", reason="Missing or ambiguous provider pricing.")
            elif segment.get("reference_scenario") is None or segment.get("gap_interval_percentage_points") is None or not total:
                candidate["reason"] = "Not enough comparable history or eligible volume for a priced benchmark."
            else:
                n = segment["current_completed_orders"]
                ratio = Decimal(economics.eligible_orders)/Decimal(total)
                full = Decimal(str(min(total, n*max(0, segment["benchmark_success_rate"]-segment["current_success_rate"]))))
                bounds = [min(Decimal(total), Decimal(n)*max(Decimal(0), Decimal(str(pp))/100))
                          for pp in segment["gap_interval_percentage_points"]]
                candidate["full_gap_successes"] = str(full*ratio)
                candidate["historical_gap_bounds_successes"] = [str(value*ratio) for value in bounds]
                scenario = action_scenario(candidate, settings)
                lower, upper = scenario["net_impact_interval_paise"]
                candidate.update(scenario=scenario, conservative_net_paise=lower,
                                 expected_net_paise=scenario["result"]["incremental_net_contribution_paise"])
                if segment["gap_interval_percentage_points"][0] <= 0:
                    candidate.update(status="NO_CLEAR_DECLINE", reason="A comparable historical decline is not established.")
                elif upper <= 0:
                    candidate.update(status="NOT_PROFITABLE", reason="Recovery does not cover additional costs under these assumptions.")
                elif lower <= 0:
                    candidate.update(status="UNCERTAIN_FINANCIAL_IMPACT", reason="The conditional historical range crosses zero.")
                else:
                    candidate.update(status="CANDIDATE_FOR_TEST", reason="Positive lower conditional net bound; validate recovery effectiveness with a test.")
            actions.append(candidate)
    eligible = sorted((a for a in actions if a["status"] == "CANDIDATE_FOR_TEST"),
                      key=lambda a: (-a["conservative_net_paise"], -a["expected_net_paise"], a["eligible_orders"], a["action_id"]))
    for index, action in enumerate(eligible, 1):
        action["rank"] = index
    return sorted(actions, key=lambda a: (a["rank"] is None, a["rank"] or 0, a["action_id"]))


def action_request(report):
    if report.get("segments"):
        return AnalysisRequest.model_validate(report["filters"])
    extra = _current_bank_method_scope(report.get("sql_result", {}).get("generated_sql", ""))
    if extra is None:
        raise AnalysisBlocked("Action ranking needs the current snapshot by bank/payment method. Ask which payment issue to fix first; custom date/category questions are not broadened.")
    # A daily/category grouping partitions the same current cohort; it does not
    # widen it. Extra WHERE predicates remain rejected by the scope parser.
    scope = {**report["filters"]}
    for column, name in (("initial_bank", "banks"), ("initial_payment_method", "payment_methods")):
        if column in extra:
            if scope.get(name) and extra[column] not in scope[name]:
                raise AnalysisBlocked("The question's SQL conflicts with its selected scope.")
            scope[name] = [extra[column]]
    return AnalysisRequest.model_validate(scope)


class NextActionsWorkflow:
    __slots__ = ("_reader",)

    def __init__(self, reader=None):
        self._reader = reader or ReadOnlyActionResearch()

    def run(self, report, settings):
        started = perf_counter()
        request = action_request(report)
        if not report.get("segments"):
            raise AnalysisBlocked("A reconciled canonical benchmark is required before researching actions.")
        # The workflow only uses this read-only capability; the outer API supplies
        # a canonical snapshot and separately saves the completed plan.
        rows, evidence = self._reader.read(request)
        actions = rank_actions(report, rows, settings)
        duration = round((perf_counter()-started)*1000)
        investigations = [{"segment_id": s["segment_id"], "title": f"Review {s['bank']} {s['payment_method']} pricing",
                           "observed_net_paise": s["current_net_contribution_paise"],
                           "reason": "Current contribution is negative. A new fee/cost quote is required before assigning a benefit to a pricing or routing change."}
                          for s in report["segments"] if s["current_net_contribution_paise"] < 0]
        return {"calculation_version": ACTION_VERSION, "dataset": report["dataset"], "filters": request.model_dump(mode="json"),
            "settings": settings.model_dump(mode="json"), "actions": actions, "ranked_count": sum(a["rank"] is not None for a in actions),
            "investigations": investigations, "baseline": {"extra_net_paise": 0, "additional_successful_orders": 0},
            "research": {"agent": "next_action_research", "permissions": "SELECT only", "database_role": "payment_analyst_research",
                         "failure_groups_checked": len(rows), "eligible_payments_checked": sum(int(r["eligible_orders"]) for r in rows), "rows": rows},
            "query_evidence": [evidence], "workflow_steps": [
                {"id": "action-research", "title": "Research eligible failures and prices", "status": "completed", "duration_ms": duration,
                 "detail": "Read-only research grouped actual network/timeout failures and reconciled counts and fee/cost totals."},
                {"id": "action-rank", "title": "Compare action alternatives after costs", "status": "completed",
                 "detail": "Ranked only positive lower conditional net bounds; weak evidence, missing prices and loss-making alternatives are withheld."}],
            "ranking_method": "Descending lower historical net-impact bound at the same assumed recovery and fixed cost. Ties: point net, fewer attempted retries, stable action ID. No arbitrary weighted score.",
            "assumptions": ["Recovery fraction is assumed, not measured causal effectiveness.",
                "The historical gap is allocated uniformly across eligible network/timeout failures; code-specific treatment effects are unknown.",
                "Alternative actions overlap. Choose one per segment; do not add their benefits.",
                "An eligible-failure cap can collapse the conditional range; this does not make recovery effectiveness certain.",
                "Scenarios use the observed current-snapshot backlog and as-of prices, not a monthly revenue forecast.",
                "Historical bootstrap bounds are approximate and conditional on the action and cost assumptions; fixed overhead is excluded."],
            "limits": "Only supported retry alternatives are financially scored. Provider switching, pricing changes and engineering fixes need verified costs/effectiveness before receiving a benefit estimate."}

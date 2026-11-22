"""Matched historical comparisons and conservative, conditional opportunity ranking."""

from collections import defaultdict
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256

import numpy as np

from .contracts import (AnalysisBlocked, AnalysisRequest, CALCULATION_VERSION,
                        EngineSettings, ScenarioRequest, Snapshot)
from .data import IST
from .finance import RetryEconomics, simulate

ECONOMICS_COLUMNS = {
    "eligible_orders": "eligible_orders",
    "eligible_payment_value_paise": "eligible_payment_value_paise",
    "merchant_fee_if_all_recovered_paise": "retry_fee_paise",
    "processing_cost_if_all_succeed_paise": "retry_success_cost_paise",
    "processing_cost_if_all_fail_paise": "retry_failure_cost_paise",
    "loss_provision_if_all_recovered_paise": "retry_loss_provision_paise",
}


def _cell(row):
    return (row["business_date"].weekday(), row["merchant_category"], row["amount_band"])


def _total(rows, column):
    return sum(int(row[column]) for row in rows)


def _validate_snapshot(snapshot):
    d = snapshot.diagnostics
    if int(d["order_count"]) != int(d["fact_count"]):
        raise AnalysisBlocked("Order facts are missing or duplicated. Reconcile source records first.")
    if int(d["ledger_net_paise"]) != int(d["fact_net_paise"]):
        raise AnalysisBlocked("Financial ledger and order facts disagree.")
    seen = set()
    for row in snapshot.daily:
        key = (row["bank"], row["payment_method"], row["business_date"],
               row["merchant_category"], row["amount_band"])
        if key in seen:
            raise AnalysisBlocked("Duplicate daily aggregate. Analysis would double-count orders.")
        seen.add(key)
        total, complete, success, pending, eligible = (
            int(row[c]) for c in ("total_orders", "completed_orders", "successful_orders",
                                 "pending_orders", "eligible_orders"))
        if not (total == complete + pending and 0 <= success <= complete
                and 0 <= eligible <= complete - success and pending >= 0):
            raise AnalysisBlocked("Aggregate counts or eligibility are inconsistent.")
        if not 0 <= int(row["unpriced_orders"]) <= eligible:
            raise AnalysisBlocked("Unpriced order count is inconsistent.")
        if any(int(row[column]) < 0 for column in ECONOMICS_COLUMNS.values()):
            raise AnalysisBlocked("Retry costs, values and fees must be nonnegative.")
    if not snapshot.daily:
        raise AnalysisBlocked("No orders match the selected dataset and filters.")


def _bootstrap(history, current, cells, settings, segment_id):
    """Resample whole days; historical resampling preserves weekday representation.

    Current-day weights also change current category/amount mix; each resample is
    re-matched. No Bernoulli independence assumption between same-day orders.
    """
    hist_days = sorted({r["business_date"] for r in history})
    cur_days = sorted({r["business_date"] for r in current})
    index = {cell: i for i, cell in enumerate(cells)}

    def matrices(rows, days):
        ni = np.zeros((len(days), len(cells)), dtype=float)
        si = np.zeros_like(ni)
        di = {day: i for i, day in enumerate(days)}
        for row in rows:
            cell = _cell(row)
            if cell in index:
                ni[di[row["business_date"]], index[cell]] += int(row["completed_orders"])
                si[di[row["business_date"]], index[cell]] += int(row["successful_orders"])
        return ni, si

    hn, hs = matrices(history, hist_days)
    cn, cs = matrices(current, cur_days)
    seed = settings.seed + int.from_bytes(sha256(segment_id.encode()).digest()[:4])
    rng = np.random.default_rng(seed)
    b = settings.bootstrap_samples
    hw = np.zeros((b, len(hist_days)))
    for weekday in range(7):
        positions = [i for i, day in enumerate(hist_days) if day.weekday() == weekday]
        if positions:
            hw[:, positions] = rng.multinomial(len(positions), np.full(len(positions), 1 / len(positions)), size=b)
    cw = rng.multinomial(len(cur_days), np.full(len(cur_days), 1 / len(cur_days)), size=b)
    hnb, hsb, cnb, csb = hw @ hn, hw @ hs, cw @ cn, cw @ cs
    valid = np.all((cnb == 0) | (hnb > 0), axis=1) & (cnb.sum(axis=1) > 0)
    rates = np.divide(hsb, hnb, out=np.zeros_like(hsb), where=hnb > 0)
    benchmark = (rates[valid] * cnb[valid]).sum(axis=1) / cnb[valid].sum(axis=1)
    actual = csb[valid].sum(axis=1) / cnb[valid].sum(axis=1)
    return benchmark - actual


def analyse(snapshot: Snapshot, request: AnalysisRequest,
            settings: EngineSettings = EngineSettings()) -> dict:
    _validate_snapshot(snapshot)
    if str(snapshot.version["id"]) != str(request.dataset_id):
        raise AnalysisBlocked("Dataset identity does not match the request.")
    if settings.bootstrap_samples < 100:
        raise ValueError("At least 100 bootstrap samples are required.")
    start = snapshot.version["current_start"].astimezone(IST).date()
    end = snapshot.version["window_end"].astimezone(IST).date()
    groups = defaultdict(list)
    for row in snapshot.daily:
        if request.banks and row["bank"] not in request.banks:
            continue
        if request.payment_methods and row["payment_method"] not in request.payment_methods:
            continue
        groups[(row["bank"], row["payment_method"])].append(row)
    if not groups:
        raise AnalysisBlocked("No orders match the selected filters.")
    family_size = len(groups)
    tail = settings.family_error_rate / (2 * family_size)
    segments = []
    for (bank, method), rows in sorted(groups.items()):
        sid = f"{bank}__{method}"
        history = [r for r in rows if r["business_date"] < start]
        current = [r for r in rows if start <= r["business_date"] < end]
        history_complete = [r for r in history if int(r["completed_orders"]) > 0]
        current_complete = [r for r in current if int(r["completed_orders"]) > 0]
        hn, cn = _total(history, "completed_orders"), _total(current, "completed_orders")
        hs, cs = _total(history, "successful_orders"), _total(current, "successful_orders")
        economics = RetryEconomics(**{field: _total(current, column)
                                     for field, column in ECONOMICS_COLUMNS.items()})
        segment = {
            "segment_id": sid, "bank": bank, "payment_method": method, "rank": None,
            "status": "INSUFFICIENT_EVIDENCE", "reasons": [],
            "historical_completed_orders": hn, "current_completed_orders": cn,
            "current_successful_orders": cs, "pending_orders": _total(current, "pending_orders"),
            "raw_historical_success_rate": hs / hn if hn else None,
            "current_success_rate": cs / cn if cn else None,
            "current_net_contribution_paise": _total(current, "net_contribution_paise"),
            "economics": asdict(economics), "benchmark_success_rate": None,
            "gap_percentage_points": None, "gap_interval_percentage_points": None,
            "reference_scenario": None, "net_impact_interval_paise": None,
            "benchmark_coverage": 0.0, "trend": [],
        }
        if segment["pending_orders"]:
            segment["reasons"].append("Pending orders are excluded from the success-rate denominator.")
        hist_cells = defaultdict(lambda: [0, 0])
        for row in history_complete:
            values = hist_cells[_cell(row)]
            values[0] += int(row["completed_orders"])
            values[1] += int(row["successful_orders"])
        cur_cells = defaultdict(int)
        for row in current_complete:
            cur_cells[_cell(row)] += int(row["completed_orders"])
        supported = {cell: values[1] / values[0] for cell, values in hist_cells.items()
                     if values[0] >= settings.min_stratum_history}
        covered = sum(n for cell, n in cur_cells.items() if cell in supported)
        segment["benchmark_coverage"] = covered / cn if cn else 0

        daily_groups = defaultdict(list)
        for row in rows:
            daily_groups[row["business_date"]].append(row)
        for day, day_rows in sorted(daily_groups.items()):
            n = _total(day_rows, "completed_orders")
            matched = (sum(int(r["completed_orders"]) * supported.get(_cell(r), 0) for r in day_rows) / n
                       if n and day >= start and all(_cell(r) in supported
                           for r in day_rows if int(r["completed_orders"])) else None)
            segment["trend"].append({"date": day.isoformat(), "completed_orders": n,
                "success_rate": _total(day_rows, "successful_orders") / n if n else None,
                "matched_historical_rate": matched, "is_current_window": day >= start})

        enough = (cn >= settings.min_current_orders and hn >= settings.min_history_orders
                  and len({r["business_date"] for r in history_complete}) >= settings.min_history_days
                  and len({r["business_date"] for r in current_complete}) >= settings.min_current_days
                  and covered == cn and cn > 0)
        if not enough:
            segment["reasons"].append("Insufficient volume, day coverage or matched weekday/category/amount history.")
            segments.append(segment)
            continue
        benchmark = sum(n * supported[cell] for cell, n in cur_cells.items()) / cn
        gap = benchmark - cs / cn
        segment["benchmark_success_rate"] = benchmark
        segment["gap_percentage_points"] = 100 * gap
        if _total(current, "unpriced_orders"):
            segment["status"] = "UNPRICED"
            segment["reasons"].append("Retry pricing is missing or overlapping at the observation cutoff.")
            segments.append(segment)
            continue
        if not economics.eligible_orders:
            segment["status"] = "NO_RETRY_ELIGIBLE_ORDERS"
            segment["reasons"].append("No unresolved network/timeout orders qualify for the retry scenario.")
            segments.append(segment)
            continue

        samples = _bootstrap(history_complete, current_complete, sorted(cur_cells), settings, sid)
        if len(samples) < settings.bootstrap_samples * 0.95:
            segment["reasons"].append("Too many bootstrap samples lack comparable historical cells.")
            segments.append(segment)
            continue
        low_gap, high_gap = np.quantile(samples, [tail, 1 - tail])
        full_gap = min(economics.eligible_orders, cn * max(0, gap))
        reference = simulate(economics, full_gap, Decimal(1), request.intervention_cost_paise)
        qs = np.minimum(economics.eligible_orders, cn * np.maximum(0, samples)) / economics.eligible_orders
        slope = (economics.merchant_fee_if_all_recovered_paise
                 - economics.processing_cost_if_all_succeed_paise
                 - economics.loss_provision_if_all_recovered_paise
                 + economics.processing_cost_if_all_fail_paise)
        net_samples = qs * slope - economics.processing_cost_if_all_fail_paise - request.intervention_cost_paise
        lower_net, upper_net = np.quantile(net_samples, [tail, 1 - tail])
        segment.update({
            "gap_interval_percentage_points": [float(low_gap * 100), float(high_gap * 100)],
            "reference_scenario": reference,
            "net_impact_interval_paise": [int(np.floor(lower_net)), int(np.ceil(upper_net))],
            "bootstrap_samples_used": len(samples),
            "eligible_failure_cap_binds": cn * max(0, gap) >= economics.eligible_orders,
            "bootstrap_cap_binding_fraction": float(np.mean(cn * np.maximum(0, samples) >= economics.eligible_orders)),
        })
        if segment["bootstrap_cap_binding_fraction"] > 0.5:
            segment["reasons"].append("Eligible-failure cap compresses the financial range; this does not imply certain recovery.")
        if low_gap <= 0:
            segment["status"] = "NO_CLEAR_DECLINE"
            segment["reasons"].append("Matched performance gap is not clearly positive after uncertainty adjustment.")
        elif upper_net <= 0:
            segment["status"] = "NOT_PROFITABLE"
            segment["reasons"].append("Additional retry fees/costs outweigh recovery value in the reference scenario.")
        elif lower_net <= 0:
            segment["status"] = "UNCERTAIN_FINANCIAL_IMPACT"
            segment["reasons"].append("Financial range crosses zero; do not treat this as a reliable priority.")
        else:
            segment["status"] = "ACTIONABLE"
            segment["reasons"].append("Conservative conditional impact is positive; validate the action with an experiment.")
        segments.append(segment)

    actionable = sorted([s for s in segments if s["status"] == "ACTIONABLE"],
                        key=lambda s: (-s["net_impact_interval_paise"][0], s["segment_id"]))
    for rank, segment in enumerate(actionable, 1):
        segment["rank"] = rank
        segment["ranking_uncertain"] = any(
            other["segment_id"] != segment["segment_id"]
            and other["net_impact_interval_paise"][0] <= segment["net_impact_interval_paise"][1]
            and segment["net_impact_interval_paise"][0] <= other["net_impact_interval_paise"][1]
            for other in actionable)
    segments.sort(key=lambda s: (s["rank"] is None, s["rank"] or 0, s["segment_id"]))
    return {
        "calculation_version": CALCULATION_VERSION,
        "dataset": {k: str(v) if k in {"id", "history_start", "current_start", "window_end", "as_of"} else v
                    for k, v in snapshot.version.items() if k != "config"},
        "source": snapshot.source,
        "filters": request.model_dump(mode="json"),
        "method": {
            "benchmark": "Historical weekday × merchant category × amount band, weighted to current completed orders.",
            "reference_scenario": "Recover 100% of the observed matched gap, capped by eligible failures; retry all eligible orders once.",
            "ranking": "Descending lower net-impact bound; unsupported, unprofitable and uncertain cases remain unranked.",
            "uncertainty": "Whole-day bootstrap; historical resampling stratified by weekday; Bonferroni-adjusted percentile bounds.",
            "family_size": family_size, "family_error_rate": settings.family_error_rate,
            "settings": asdict(settings),
        },
        "segments": segments,
        "charts": {"ranking": [{"segment_id": s["segment_id"], "rank": s["rank"],
            "status": s["status"], "net_paise": s["reference_scenario"]["incremental_net_contribution_paise"]
                if s["reference_scenario"] else None,
            "interval_paise": s["net_impact_interval_paise"]} for s in segments]},
        "query_evidence": snapshot.query_evidence,
        "caveats": [
            "All demonstration data and prices are synthetic.",
            "Historical recoverability is a scenario benchmark, not evidence that retries cause recovery.",
            "Bootstrap ranges describe historical sampling variability conditional on the cost/action assumptions.",
            "Seven current days provide limited evidence; adjusted bootstrap bounds are approximate, not guaranteed coverage.",
            "Net contribution includes modelled loss provision and excludes fixed overhead; it is not total profit.",
        ],
    }


def scenario_from_analysis(analysis: dict, request: ScenarioRequest) -> dict:
    segment = next((s for s in analysis["segments"] if s["segment_id"] == request.segment_id), None)
    if not segment or segment["reference_scenario"] is None:
        raise AnalysisBlocked("No supported, fully priced benchmark exists for this segment.")
    # Recompute from original rates; never use the rounded displayed recovery count.
    n = segment["current_completed_orders"]
    full_gap = min(segment["economics"]["eligible_orders"],
                   n * max(0, segment["benchmark_success_rate"] - segment["current_success_rate"]))
    economics = RetryEconomics(**segment["economics"])
    result = simulate(economics, full_gap, request.recovery_fraction, request.intervention_cost_paise)
    curve = [simulate(economics, full_gap, Decimal(i) / 20, request.intervention_cost_paise)
             for i in range(21)]
    return {
        "segment_id": request.segment_id, "calculation_version": CALCULATION_VERSION,
        "dataset": analysis["dataset"], "benchmark_status": segment["status"],
        "current_success_rate": segment["current_success_rate"],
        "benchmark_success_rate": segment["benchmark_success_rate"],
        "result": result,
        "curve": [{"recovery_fraction": p["recovery_fraction"],
                   "incremental_net_contribution_paise": p["incremental_net_contribution_paise"],
                   "additional_successful_orders": p["additional_successful_orders"]} for p in curve],
        "caveats": analysis["caveats"],
    }

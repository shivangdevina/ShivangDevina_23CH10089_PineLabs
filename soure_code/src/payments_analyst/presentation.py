"""Evidence-bound prose and KPI presentation. No model-generated facts or amounts."""

from decimal import Decimal
from math import isfinite
from datetime import date


ANSWER_VERSION = "plain-language-research-v3"


def _numeric(value):
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and isfinite(value)


def _display(column, value):
    if value is None:
        return "not available"
    if not _numeric(value):
        return str(value)
    if column.endswith("_paise"):
        return f"INR {Decimal(str(value)) / 100:,.2f}"
    if column.endswith("_pct"):
        return f"{value:,.2f}%"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _label(column):
    return column.removesuffix("_paise").removesuffix("_pct").replace("_", " ")


def _subject(row, dimensions):
    values = []
    for col in dimensions:
        value = row.get(col)
        if value is None:
            continue
        if col == "business_date":
            try:
                value = date.fromisoformat(str(value)[:10]).strftime("%d %b").lstrip("0")
            except ValueError:
                pass
        values.append(str(value).replace("CARD", "Card"))
    return " ".join(values) or "Across the payments in this result"


def _plain_row(row, dimensions, metrics):
    name = _subject(row, dimensions)
    n = row.get("completed_orders", row.get("completed_orders_count"))
    s = row.get("successful_orders", row.get("successful_orders_count"))
    rate = row.get("success_rate_pct")
    parts = []
    if _numeric(n) and _numeric(s):
        parts.append(f"{_display('count', s)} of {_display('count', n)} completed payments succeeded" + (f" ({rate:.2f}%)" if _numeric(rate) else "") + ".")
    elif _numeric(rate):
        parts.append(f"{rate:.2f}% of completed payments succeeded.")
    elif _numeric(n):
        parts.append(f"{_display('count', n)} payments completed.")
    if "net_contribution_paise" in row:
        net = row["net_contribution_paise"]
        if _numeric(net):
            parts.append(f"Money left after payment costs and estimated losses: {_display('net_contribution_paise', net)}.")
        else:
            parts.append("Money left after payment costs is not available in this result.")
    if not parts:
        parts = [f"{_label(col).capitalize()}: {_display(col, row.get(col))}." for col in (metrics or list(row))[:4]]
    return f"{name}: " + " ".join(parts)


def _no_verified_cause(facts):
    return [{"id": "E1", "kind": "explanation", "status": "not_established",
             "text": "This comparison shows the outcome, but does not show which underlying event caused it. A cause needs a supporting check, such as a cost breakdown or recorded failure codes.",
             "evidence_ids": [fact["id"] for fact in facts]}]


def _verified_accounting(rows, facts):
    """Confirm an accounting explanation only when all four observed totals reconcile."""
    columns = ("merchant_fee_paise", "processing_cost_paise", "loss_provision_paise", "net_contribution_paise")
    indexes = sorted(range(len(rows)), key=lambda i: not (_numeric(rows[i].get("net_contribution_paise")) and rows[i]["net_contribution_paise"] < 0))
    for index in indexes:
        row = rows[index]
        if not all(_numeric(row.get(col)) for col in columns):
            continue
        fee, cost, loss, net = (Decimal(str(row[col])) for col in columns)
        if min(fee, cost, loss) < 0 or fee - cost - loss != net:
            continue
        related = [fact["id"] for fact in facts
                   if any(ref.get("row_index") == index for ref in fact.get("evidence", []))]
        if not related:
            continue
        name = " / ".join(str(row[col]) for col in ("initial_bank", "initial_payment_method", "merchant_category", "business_date", "period")
                          if row.get(col) is not None) or "This result"
        text = (f"{name}: payment costs of {_display('processing_cost_paise', cost)} plus estimated losses of {_display('loss_provision_paise', loss)} exceed fees earned of {_display('merchant_fee_paise', fee)}. That leaves {_display('net_contribution_paise', net)} after these costs."
                if net < 0 else f"{name}: fees earned of {_display('merchant_fee_paise', fee)}, less payment costs of {_display('processing_cost_paise', cost)} and estimated losses of {_display('loss_provision_paise', loss)}, leave {_display('net_contribution_paise', net)}.")
        return [{"id": "E1", "kind": "explanation", "status": "verified_data", "text": text,
                 "evidence_ids": related,
                 "evidence": [{"query_id": "gemini-generated-sql", "row_index": index, "columns": list(columns)}]}]
    return _no_verified_cause(facts)


def present_query(report: dict) -> dict:
    """Describe actual result cells; never ask the model to manufacture findings."""
    result = report["sql_result"]
    rows, columns = result["rows"], result["columns"]
    metrics = [col for col in columns if any(_numeric(row.get(col)) for row in rows)]
    dimensions = [col for col in columns if col not in metrics]
    indexes = list(range(min(3, len(rows))))
    if "business_date" in dimensions and len(rows) > 3:
        indexes = [0, len(rows) - 1]
    negative = next((i for i, row in enumerate(rows)
                     if _numeric(row.get("net_contribution_paise")) and row["net_contribution_paise"] < 0), None)
    if negative is not None and negative not in indexes:
        indexes.append(negative)
    facts = []
    for index in indexes:
        row = rows[index]
        priorities = [col for col in ("net_contribution_paise", "success_rate_pct") if col in metrics]
        described = (priorities + [col for col in metrics if col not in priorities])[:4] or columns[:4]
        facts.append({"id": f"F{len(facts) + 1}", "kind": "observed", "text": _plain_row(row, dimensions, described),
                      "source": f"Executed SQL result, row {index + 1}.",
                      "evidence": [{"query_id": "gemini-generated-sql", "row_index": index,
                                    "columns": list(dict.fromkeys([*dimensions, *described]))}]})
    if not facts:
        facts.append({"id": "F1", "kind": "observed", "text": "The executed query returned no matching result rows.",
                      "source": "Executed SQL result count.", "evidence": [{"query_id": "gemini-generated-sql", "row_count": 0}]})
    explanations = _verified_accounting(rows, facts)
    if dimensions == ["business_date"] and len(rows) > 1 and len({row["business_date"] for row in rows}) == len(rows):
        ordered = sorted(enumerate(rows), key=lambda item: str(item[1]["business_date"]))
        (first_index, first), (last_index, last) = ordered[0], ordered[-1]
        metric = "net_contribution_paise" if "net_contribution_paise" in metrics else "success_rate_pct" if "success_rate_pct" in metrics else None
        if metric and _numeric(first.get(metric)) and _numeric(last.get(metric)):
            change = Decimal(str(last[metric])) - Decimal(str(first[metric]))
            verb = "increased" if change > 0 else "decreased" if change < 0 else "stayed unchanged"
            subject = "Money left after payment costs and estimated losses" if metric.endswith("_paise") else "Payment success rate"
            explanations = [{"id": "E1", "kind": "explanation", "status": "verified_data",
                "text": f"{subject} {verb} from {_display(metric, first[metric])} on {_subject(first, dimensions)} to {_display(metric, last[metric])} on {_subject(last, dimensions)}. These are the first and last observed days; the comparison alone does not identify the cause.",
                "evidence_ids": [fact["id"] for fact in facts],
                "evidence": [{"query_id": "gemini-generated-sql", "row_index": i, "columns": ["business_date", metric]} for i in (first_index, last_index)]}]
    if not rows:
        next_step = "Check the selected snapshot and filters, then broaden the question to look for matching observations."
        related = ["F1"]
    elif negative is not None:
        next_step = "Review the negative-contribution group's fees and processing charges first. Compare the expected benefit after all retry costs before deciding to retry more payments."
        related = [fact["id"] for fact in facts if fact["evidence"][0].get("row_index") == negative]
    elif report["planning"]["plan"]["intent"] == "trend":
        next_step = "Compare the same weekdays and similar payment types, then check whether fewer payments completed or the cost per payment changed."
        related = [fact["id"] for fact in facts]
    else:
        next_step = "Compare each bank with similar earlier payments. Then rank fixes by the extra money they could retain after costs, rather than by success rate alone."
        related = [fact["id"] for fact in facts]
    answer = {"version": ANSWER_VERSION, "title": "Here is what your data shows.",
              "facts": facts, "hypotheses": [], "explanations": explanations,
              "recommendations": [{"id": "R1", "kind": "recommendation", "text": next_step,
                                   "evidence_ids": related}]}
    return {**report, "answer": answer}


def apply_research(report, research):
    """Turn verified supporting rows into specific statements, without model-written numbers."""
    report["research"] = research
    report["query_evidence"].extend(research.get("query_evidence", []))
    if research.get("status") != "completed":
        return report
    answer = report["answer"]
    rows = research.get("result", {}).get("rows", [])
    purpose = research.get("purpose")
    if purpose == "cost_breakdown":
        expected = research.get("expected_target_net_paise")
        keys = [(row.get("initial_bank"), row.get("initial_payment_method")) for row in rows]
        if (not rows or not _numeric(expected) or len(set(keys)) != len(keys) or
            any(bank != research["target_bank"] or method not in {"UPI", "CARD"} or
                (research.get("target_method") and method != research["target_method"]) for bank, method in keys) or
            any(not _numeric(row.get("net_contribution_paise")) for row in rows) or
            sum(Decimal(str(row["net_contribution_paise"])) for row in rows) != Decimal(str(expected))):
            research["verification"] = "Research costs did not reconcile to the original current-snapshot group; no extra conclusion was made."
            research["status"] = "withheld"
            research["summary"] = "The supporting costs did not match the original group; the answer uses the primary results only."
            return report
        linked = []
        for index, row in enumerate(rows):
            if not all(_numeric(row.get(col)) for col in ("merchant_fee_paise", "processing_cost_paise", "loss_provision_paise", "net_contribution_paise")):
                continue
            identifier = f"F{len(answer['facts']) + 1}"
            fact = {"id": identifier, "kind": "observed", "text": "Current-snapshot follow-up — " + _plain_row(row, ["initial_bank", "initial_payment_method"], []),
                    "source": "Read-only research: fees and costs by bank/payment method in the current snapshot.",
                    "evidence": [{"query_id": "research-agent-sql", "row_index": index, "columns": list(row)}]}
            answer["facts"].append(fact)
            linked.append(fact)
        explanations = _verified_accounting(rows, linked)
        if explanations[0]["status"] == "verified_data":
            explanations[0]["text"] = "In the current-snapshot follow-up, " + explanations[0]["text"]
            explanations[0]["evidence"][0]["query_id"] = "research-agent-sql"
            answer["explanations"] = explanations
            answer["recommendations"][0]["text"] = f"Review {research['target_bank']}'s provider charges and merchant fees before sending it more payments. A higher success rate will not help financially if the extra payment costs exceed the extra fees earned."
            answer["recommendations"][0]["evidence_ids"] = explanations[0]["evidence_ids"]
    elif purpose == "failure_codes":
        allowed = {"NETWORK_ERROR", "PROVIDER_TIMEOUT", "INSUFFICIENT_FUNDS", "DO_NOT_HONOR"}
        if not rows or any(row.get("last_failure_code") not in allowed or not _numeric(row.get("failed_orders")) or row["failed_orders"] < 0 or int(row["failed_orders"]) != row["failed_orders"] for row in rows):
            research["status"] = "withheld"
            research["summary"] = "The failure-code check did not return verifiable counts; no extra conclusion was made."
            return report
        if len({row["last_failure_code"] for row in rows}) != len(rows):
            research["status"] = "withheld"
            research["summary"] = "Duplicate failure-code groups prevented a reliable extra conclusion."
            return report
        target = next((s for s in report["segments"] if s["bank"] == research["target_bank"] and s["payment_method"] == research["target_method"]), None)
        total = sum(int(row["failed_orders"]) for row in rows)
        if target is None or total != target["current_completed_orders"] - target["current_successful_orders"]:
            research["verification"] = "Supporting counts did not reconcile; no extra conclusion was made."
            research["status"] = "withheld"
            research["summary"] = "The failure-code counts did not match the primary totals; no extra conclusion was made."
            return report
        retryable = sum(int(row["failed_orders"]) for row in rows if row["last_failure_code"] in {"NETWORK_ERROR", "PROVIDER_TIMEOUT"})
        identifier = f"F{len(answer['facts']) + 1}"
        name = f"{research['target_bank']} {research['target_method']}"
        answer["facts"].append({"id": identifier, "kind": "observed",
            "text": f"For {name}, {total:,} completed payments failed in the current snapshot. {retryable:,} of these recorded a network error or provider timeout.",
            "source": "Read-only research: current failed-order counts by last failure code, reconciled to completed minus successful payments.",
            "evidence": [{"query_id": "research-agent-sql", "row_index": i, "columns": ["last_failure_code", "failed_orders"]} for i in range(len(rows))]})
        answer["explanations"].append({"id": f"E{len(answer['explanations']) + 1}", "kind": "explanation", "status": "verified_data",
            "text": f"The failure records identify {retryable:,} network/timeout failures worth investigating for {name}. The codes describe what was reported; they do not establish why the errors occurred or prove that a retry will recover them.",
            "evidence_ids": [identifier]})
        if retryable:
            answer["recommendations"][0]["text"] = f"Start by reviewing the {retryable:,} network/timeout failures for {name}. Test retries on a small eligible group and measure money retained after retry costs before changing routing."
            answer["recommendations"][0]["evidence_ids"] = [identifier]
    return report


def _percent(value):
    return f"{value * 100:.1f}%"


def _money(paise):
    return f"INR {paise / 100:,.2f}"


def present(report: dict, question: str, planning: dict) -> dict:
    segments = report["segments"]
    n = sum(s["current_completed_orders"] for s in segments)
    successes = sum(s["current_successful_orders"] for s in segments)
    pending = sum(s["pending_orders"] for s in segments)
    net = sum(s["current_net_contribution_paise"] for s in segments)
    overview = {"completed_orders": n, "successful_orders": successes,
                "success_rate": successes / n if n else None, "pending_orders": pending,
                "net_contribution_paise": net,
                "ranked_opportunities": sum(s["rank"] is not None for s in segments)}
    facts = [{"id": "F1", "kind": "observed", "text": (
        f"{successes:,} of {n:,} completed payments succeeded ({_percent(successes / n)}). "
        f"{pending:,} payments are still pending and are excluded from this rate." if n else "No completed payments match your question."),
        "source": "Selected current-window order aggregates."}]
    intent = planning["plan"]["intent"]
    supported = [s for s in segments if s["benchmark_success_rate"] is not None]
    if intent == "drivers":
        supported.sort(key=lambda s: -(s["gap_percentage_points"] or 0))
    else:
        supported.sort(key=lambda s: (s["rank"] is None, s["rank"] or 0, s["segment_id"]))
    for segment in supported[:2]:
        facts.append({"id": f"F{len(facts) + 1}", "kind": "observed",
            "segment_id": segment["segment_id"],
            "text": (f"{segment['bank']} / {segment['payment_method']}: "
                     f"{segment['current_successful_orders']:,} of {segment['current_completed_orders']:,} completed payments succeeded ({_percent(segment['current_success_rate'])}). "
                     f"Similar earlier payments succeeded at {_percent(segment['benchmark_success_rate'])}."),
            "source": "Weekday/category/amount-matched historical order aggregates."})
    ranked = [s for s in segments if s["rank"] is not None]
    recommendations = []
    for segment in ranked[:2]:
        if not any(f.get("segment_id") == segment["segment_id"] for f in facts):
            facts.append({"id": f"F{len(facts) + 1}", "kind": "observed", "segment_id": segment["segment_id"],
                          "text": f"{segment['bank']} / {segment['payment_method']} has {_percent(segment['current_success_rate'])} current conversion versus {_percent(segment['benchmark_success_rate'])} matched historical conversion.",
                          "source": "Matched historical order aggregates."})
        reference = segment["reference_scenario"]
        recommendations.append({"id": f"R{len(recommendations) + 1}", "kind": "recommendation",
            "segment_id": segment["segment_id"],
            "text": f"Start with {segment['bank']} {segment['payment_method']}: review the failed-payment responses and run a small retry test before changing live payment routing.",
            "scenario_note": f"If extra retries close the full observed gap, the model estimates {_money(reference['incremental_net_contribution_paise'])} more after costs. This is a what-if estimate; actual recovery must be tested.",
            "evidence_ids": [f["id"] for f in facts if f.get("segment_id") == segment["segment_id"]]})
    if not recommendations:
        recommendations.append({"id": "R1", "kind": "recommendation",
            "text": "There is not enough evidence to recommend a profitable fix yet. Check missing history, small samples and fees before spending money on a change.",
            "scenario_note": "An unranked segment does not establish that there is no business issue.", "evidence_ids": ["F1"]})
    explanations = []
    for segment in segments:
        interval = segment.get("gap_interval_percentage_points")
        related = [fact["id"] for fact in facts if fact.get("segment_id") == segment["segment_id"]]
        if interval and interval[0] > 0 and related:
            explanations.append({"id": f"E{len(explanations) + 1}", "kind": "explanation", "status": "verified_data",
                "text": f"{segment['bank']} {segment['payment_method']}: the success rate is still lower when we compare similar weekdays, merchant types and payment amounts. This supports a performance gap for comparable payments; its underlying cause is not verified.",
                "evidence_ids": related, "source": "Matched benchmark and positive sampling-range lower bound."})
    if not explanations:
        explanations = _no_verified_cause(facts)
    title = {"priorities": "Start with the strongest supported opportunity.",
             "drivers": "Here is what the data confirms.",
             "trend": "Compare conversion with a matched baseline.",
             "overview": "Here is what your payment data shows."}[intent]
    if intent == "priorities" and not ranked:
        title = "No supported priority identified."
    elif ranked:
        title = f"Start with {ranked[0]['bank']} {ranked[0]['payment_method']}."
    return {**report, "question": question, "planning": planning, "overview": overview,
            "answer": {"version": ANSWER_VERSION, "title": title, "facts": facts, "hypotheses": [], "explanations": explanations,
                       "recommendations": recommendations}}

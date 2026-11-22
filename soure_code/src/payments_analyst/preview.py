"""Credential-free engine exercise. This is NOT a database or API fallback."""

from collections import defaultdict

from .contracts import Snapshot
from .data import IST, RETRYABLE_FAILURES, Dataset, basis_points
from .validation import validate


def snapshot_from_generated(data: Dataset) -> Snapshot:
    validate(data)
    merchants = {m["id"]: m for m in data.tables["merchants"]}
    attempts = defaultdict(list)
    ledger = defaultdict(int)
    for attempt in data.tables["payment_attempts"]:
        attempts[attempt["order_id"]].append(attempt)
    for event in data.tables["financial_events"]:
        ledger[event["order_id"]] += event["amount_paise"]
    daily = {}
    for order in data.tables["orders"]:
        merchant = merchants[order["merchant_id"]]
        day = order["created_at"].astimezone(IST).date()
        band = "UNDER_500" if order["amount_paise"] < 50_000 else "500_PLUS"
        key = (order["initial_bank"], order["initial_payment_method"], day, merchant["category"], band)
        if key not in daily:
            daily[key] = dict(zip(("bank", "payment_method", "business_date", "merchant_category", "amount_band"), key))
            daily[key].update({k: 0 for k in ("total_orders", "completed_orders", "successful_orders",
                "pending_orders", "eligible_orders", "unpriced_orders", "eligible_payment_value_paise",
                "retry_fee_paise", "retry_success_cost_paise", "retry_failure_cost_paise",
                "retry_loss_provision_paise", "net_contribution_paise")})
        row = daily[key]
        row["total_orders"] += 1
        complete = order["finalized_at"] is not None and order["finalized_at"] <= data.version["as_of"]
        row["completed_orders" if complete else "pending_orders"] += 1
        row["successful_orders"] += int(complete and order["final_status"] == "SUCCESS")
        row["net_contribution_paise"] += ledger[order["id"]]
        last = max(attempts[order["id"]], key=lambda a: a["attempt_number"])
        eligible = complete and order["final_status"] == "FAILED" and last["failure_code"] in RETRYABLE_FAILURES
        if not eligible:
            continue
        row["eligible_orders"] += 1
        row["eligible_payment_value_paise"] += order["amount_paise"]
        rules = [r for r in data.tables["cost_rules"]
                 if r["bank"] == order["initial_bank"] and r["payment_method"] == order["initial_payment_method"]
                 and r["effective_from"] <= data.version["as_of"] < r["effective_to"]]
        if len(rules) != 1:
            row["unpriced_orders"] += 1
            continue
        rule = rules[0]
        row["retry_fee_paise"] += basis_points(order["amount_paise"], merchant["fee_bps"]) + merchant["fee_fixed_paise"]
        row["retry_success_cost_paise"] += basis_points(order["amount_paise"], rule["success_bps"]) + rule["success_fixed_paise"]
        row["retry_failure_cost_paise"] += rule["failure_fixed_paise"]
        row["retry_loss_provision_paise"] += basis_points(order["amount_paise"], rule["expected_loss_bps"])
    return Snapshot(
        version={**data.version, "fingerprint": data.fingerprint()},
        daily=list(daily.values()),
        diagnostics={"order_count": len(data.tables["orders"]), "fact_count": len(data.tables["orders"]),
                     "ledger_net_paise": sum(ledger.values()), "fact_net_paise": sum(ledger.values())},
        query_evidence=[{"execution": "NOT_EXECUTED", "note": "In-memory synthetic preview. No SQL was executed and no data was persisted."}],
        source="generated_in_memory_preview",
    )

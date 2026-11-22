"""Reject inconsistent data before any write to Supabase."""

from collections import defaultdict

from .data import Dataset, TABLES, basis_points


class DataQualityError(ValueError):
    pass


def validate(data: Dataset) -> None:
    def require(condition, message):
        if not condition:
            raise DataQualityError(message)

    maps = {}
    for table in TABLES:
        rows = data.tables[table]
        maps[table] = {r["id"]: r for r in rows}
        require(len(maps[table]) == len(rows), f"Duplicate id in {table}.")
        require(all(r["dataset_id"] == data.version["id"] for r in rows),
                f"Cross-dataset record in {table}.")

    merchants, rules, orders, attempts = (maps[t] for t in TABLES[:4])
    by_order = defaultdict(list)
    by_attempt = defaultdict(list)
    for attempt in attempts.values():
        require(attempt["order_id"] in orders, "Attempt references a missing order.")
        require(attempt["cost_rule_id"] in rules, "Missing cost rule.")
        order, rule = orders[attempt["order_id"]], rules[attempt["cost_rule_id"]]
        require((attempt["bank"], attempt["payment_method"]) == (rule["bank"], rule["payment_method"]),
                "Cost rule does not match the payment route.")
        require(rule["effective_from"] <= attempt["occurred_at"] < rule["effective_to"],
                "Cost rule is not effective at the attempt time.")
        require(order["created_at"] <= attempt["occurred_at"] <= data.version["as_of"],
                "Attempt is outside the observation window.")
        require(attempt["status"] in {"SUCCESS", "FAILED", "PENDING"}, "Unknown attempt status.")
        require((attempt["status"] == "FAILED") == (attempt["failure_code"] is not None),
                "Failure code/status mismatch.")
        by_order[attempt["order_id"]].append(attempt)

    for event in maps["financial_events"].values():
        require(event["attempt_id"] in attempts, "Ledger references a missing attempt.")
        attempt = attempts[event["attempt_id"]]
        require(event["order_id"] == attempt["order_id"], "Ledger/order mismatch.")
        require(event["occurred_at"] == attempt["occurred_at"], "Ledger timestamp mismatch.")
        by_attempt[event["attempt_id"]].append(event)

    for order in orders.values():
        require(order["merchant_id"] in merchants, "Order references a missing merchant.")
        require(order["currency"] == "INR", "Mixed currencies are unsupported.")
        require(type(order["amount_paise"]) is int and order["amount_paise"] > 0,
                "Order amount must be a positive integer in paise.")
        require(data.version["history_start"] <= order["created_at"] < data.version["window_end"],
                "Order is outside the dataset window.")
        group = sorted(by_order[order["id"]], key=lambda a: a["attempt_number"])
        require(bool(group), "Order has no attempts.")
        require([a["attempt_number"] for a in group] == list(range(1, len(group) + 1)),
                "Duplicate or non-contiguous attempt sequence.")
        require(all(a["status"] == "FAILED" for a in group[:-1]),
                "An attempt follows success or pending status.")
        require((group[0]["bank"], group[0]["payment_method"]) ==
                (order["initial_bank"], order["initial_payment_method"]), "Initial cohort mismatch.")
        require(order["final_status"] == group[-1]["status"], "Order/final attempt mismatch.")
        expected_finalized = None if order["final_status"] == "PENDING" else group[-1]["occurred_at"]
        require(order["finalized_at"] == expected_finalized, "Finalization timestamp mismatch.")
        merchant = merchants[order["merchant_id"]]
        for attempt in group:
            rule = rules[attempt["cost_rule_id"]]
            actual = by_attempt[attempt["id"]]
            amounts = {e["event_type"]: e["amount_paise"] for e in actual}
            require(len(amounts) == len(actual), "Duplicate ledger event type for attempt.")
            expected = {}
            if attempt["status"] == "SUCCESS":
                expected = {
                    "MERCHANT_FEE": basis_points(order["amount_paise"], merchant["fee_bps"]) + merchant["fee_fixed_paise"],
                    "PROCESSING_COST": -(basis_points(order["amount_paise"], rule["success_bps"]) + rule["success_fixed_paise"]),
                    "LOSS_PROVISION": -basis_points(order["amount_paise"], rule["expected_loss_bps"]),
                }
            elif attempt["status"] == "FAILED":
                expected = {"PROCESSING_COST": -rule["failure_fixed_paise"]}
            require(amounts == expected, "Financial ledger does not reconcile with fees/cost rules.")

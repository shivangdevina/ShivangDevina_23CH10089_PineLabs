"""Seeded, synthetic data; no real merchant identities or bank credentials."""

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from random import Random
from uuid import NAMESPACE_URL, uuid5

GENERATOR_VERSION = "1.0.0"
IST = timezone(timedelta(hours=5, minutes=30))
RETRYABLE_FAILURES = frozenset({"NETWORK_ERROR", "PROVIDER_TIMEOUT"})
TABLES = ("merchants", "cost_rules", "orders", "payment_attempts", "financial_events")


def basis_points(amount_paise: int, bps: int) -> int:
    """Round nonnegative money to the nearest paise, half up, without floats."""
    if amount_paise < 0 or bps < 0:
        raise ValueError("Amount and basis points must be nonnegative.")
    return (amount_paise * bps + 5_000) // 10_000


@dataclass(frozen=True)
class DatasetConfig:
    seed: int = 42
    start_date: date = date(2026, 9, 20)
    history_days: int = 56
    current_days: int = 7
    orders_per_day: int = 320
    scenario: str = "incidents"

    def __post_init__(self):
        if self.history_days < 28 or self.history_days % 7:
            raise ValueError("History must contain at least four complete weeks.")
        if self.current_days != 7:
            raise ValueError("The first version supports a seven-day current window.")
        if not 10 <= self.orders_per_day <= 10_000:
            raise ValueError("Orders per day must be between 10 and 10,000.")
        if self.scenario not in {"incidents", "healthy"}:
            raise ValueError("Scenario must be incidents or healthy.")

    def metadata(self) -> dict:
        return {**asdict(self), "start_date": self.start_date.isoformat(),
                "generator_version": GENERATOR_VERSION, "timezone": "Asia/Kolkata"}


@dataclass
class Dataset:
    version: dict
    tables: dict[str, list[dict]] = field(default_factory=lambda: {t: [] for t in TABLES})
    # Loaded into a private evaluation schema, never into analytical views.
    ground_truth: list[dict] = field(default_factory=list)

    def fingerprint(self) -> str:
        payload = {"metadata": self.version, "tables": self.tables,
                   "ground_truth": self.ground_truth}
        return sha256(json.dumps(payload, sort_keys=True, default=str,
                                 separators=(",", ":")).encode()).hexdigest()

    def summary(self) -> dict:
        events = self.tables["financial_events"]
        return {
            "synthetic": True, "dataset_id": self.version["id"],
            "fingerprint": self.fingerprint(),
            "timezone": "Asia/Kolkata",
            "current_window": [str(self.version["current_start"]), str(self.version["window_end"])],
            "counts": {table: len(rows) for table, rows in self.tables.items()},
            "order_outcomes": dict(Counter(o["final_status"] for o in self.tables["orders"])),
            "merchant_payment_value_paise": sum(o["amount_paise"] for o in self.tables["orders"]),
            "net_contribution_paise": sum(e["amount_paise"] for e in events),
            "note": "Synthetic ledger only. Ranking and simulation are subsequent steps.",
        }


def generate(config: DatasetConfig = DatasetConfig()) -> Dataset:
    """Generate reproducibly; configuration and generator version define identity."""
    rng = Random(config.seed)
    metadata = config.metadata()
    dataset_id = str(uuid5(NAMESPACE_URL, "payment-analyst:" + json.dumps(metadata, sort_keys=True)))

    def uid(value: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"{dataset_id}:{value}"))

    start = datetime.combine(config.start_date, time(), IST)
    current_start = start + timedelta(days=config.history_days)
    window_end = current_start + timedelta(days=config.current_days)
    data = Dataset({"id": dataset_id, "config": metadata, "history_start": start,
                    "current_start": current_start, "window_end": window_end,
                    "as_of": window_end, "is_synthetic": True})
    merchants = data.tables["merchants"]
    for category in ("RETAIL", "DIGITAL"):
        for index in range(6):
            merchants.append({"dataset_id": dataset_id, "id": uid(f"merchant:{category}:{index}"),
                              "name": f"Demo {category.title()} {index + 1}", "category": category,
                              "fee_bps": 80 + index * 5, "fee_fixed_paise": 5})

    # Fictional providers. Delta's CARD route deliberately has expensive processing.
    rules = {}
    for bank in ("ATLAS", "BREEZE", "CEDAR", "DELTA"):
        for method in ("UPI", "CARD"):
            expensive = bank == "DELTA" and method == "CARD"
            row = {"dataset_id": dataset_id, "id": uid(f"cost:{bank}:{method}"),
                   "bank": bank, "payment_method": method, "effective_from": start,
                   "effective_to": data.version["as_of"] + timedelta(days=1),
                   "success_bps": 250 if expensive else (25 if method == "UPI" else 45),
                   "success_fixed_paise": 8, "failure_fixed_paise": 12,
                   "expected_loss_bps": 2, "currency": "INR"}
            rules[(bank, method)] = row
            data.tables["cost_rules"].append(row)

    segments = [("ATLAS", "UPI"), ("ATLAS", "CARD"), ("BREEZE", "UPI"),
                ("BREEZE", "CARD"), ("CEDAR", "UPI"), ("CEDAR", "CARD"),
                ("DELTA", "UPI"), ("DELTA", "CARD")]
    weights = [32, 12, 8, 2, 18, 8, 8, 12]

    def event(order_id, attempt_id, kind, amount, occurred_at):
        data.tables["financial_events"].append({
            "dataset_id": dataset_id, "id": uid(f"event:{attempt_id}:{kind}"),
            "order_id": order_id, "attempt_id": attempt_id, "event_type": kind,
            "amount_paise": amount, "occurred_at": occurred_at})

    for day in range(config.history_days + config.current_days):
        current = day >= config.history_days
        for index in range(config.orders_per_day):
            bank, method = rng.choices(segments, weights)[0]
            incident = current and config.scenario == "incidents"
            mix_shift = incident and (bank, method) == ("CEDAR", "UPI")
            category = "DIGITAL" if rng.random() < (0.75 if mix_shift else 0.15) else "RETAIL"
            merchant = rng.choice([m for m in merchants if m["category"] == category])
            # Same amount distribution across categories: mix-shift evaluation stays interpretable.
            amount = rng.choice([10_000, 25_000, 50_000, 100_000, 200_000])
            created = start + timedelta(days=day, seconds=rng.randrange(86_100))
            order_id = uid(f"order:{day}:{index}")
            rule = rules[(bank, method)]
            probability = 0.97 if category == "RETAIL" else 0.79
            probability -= (created.weekday() >= 5) * 0.015
            if incident:
                probability -= {("ATLAS", "UPI"): 0.16, ("BREEZE", "CARD"): 0.32,
                                ("DELTA", "CARD"): 0.14}.get((bank, method), 0)

            order = {"dataset_id": dataset_id, "id": order_id, "merchant_id": merchant["id"],
                     "amount_paise": amount, "currency": "INR", "created_at": created,
                     "initial_bank": bank, "initial_payment_method": method,
                     "final_status": "FAILED", "finalized_at": None}
            pending = current and day == config.history_days + 6 and index % 157 == 0
            for attempt_no in (1, 2):
                attempt_id = uid(f"attempt:{day}:{index}:{attempt_no}")
                at = created + timedelta(seconds=attempt_no * 40)
                success = not pending and rng.random() < (probability if attempt_no == 1 else 0.35)
                failure = None if success or pending else rng.choice(
                    ["PROVIDER_TIMEOUT", "NETWORK_ERROR", "INSUFFICIENT_FUNDS", "DO_NOT_HONOR"])
                status = "PENDING" if pending else ("SUCCESS" if success else "FAILED")
                attempt = {"dataset_id": dataset_id, "id": attempt_id, "order_id": order_id,
                           "cost_rule_id": rule["id"], "attempt_number": attempt_no,
                           "bank": bank, "payment_method": method, "status": status,
                           "failure_code": failure, "occurred_at": at}
                data.tables["payment_attempts"].append(attempt)
                if pending:
                    order["final_status"] = "PENDING"
                    break
                cost = (basis_points(amount, rule["success_bps"]) + rule["success_fixed_paise"]
                        if success else rule["failure_fixed_paise"])
                event(order_id, attempt_id, "PROCESSING_COST", -cost, at)
                order["finalized_at"] = at
                if success:
                    order["final_status"] = "SUCCESS"
                    event(order_id, attempt_id, "MERCHANT_FEE",
                          basis_points(amount, merchant["fee_bps"]) + merchant["fee_fixed_paise"], at)
                    # An explicit synthetic expected-loss provision; never called a realised loss.
                    event(order_id, attempt_id, "LOSS_PROVISION",
                          -basis_points(amount, rule["expected_loss_bps"]), at)
                    break
                if attempt_no == 1 and failure in RETRYABLE_FAILURES and rng.random() < 0.35:
                    continue
                break
            data.tables["orders"].append(order)

    if config.scenario == "incidents":
        for bank, method, kind, description in [
            ("ATLAS", "UPI", "PERFORMANCE_DROP", "High-volume route: current per-attempt success probability decreases by 16 percentage points."),
            ("BREEZE", "CARD", "LOW_VOLUME_DROP", "Low-volume route: current per-attempt success probability decreases by 32 percentage points."),
            ("CEDAR", "UPI", "MIX_SHIFT", "DIGITAL category share rises; conditional success probabilities are unchanged."),
            ("DELTA", "CARD", "EXPENSIVE_RECOVERY", "Processing rate exceeds merchant fee rates; successful recovery can lose money."),
        ]:
            data.ground_truth.append({"dataset_id": dataset_id, "bank": bank,
                                      "payment_method": method, "kind": kind,
                                      "description": description})
    return data

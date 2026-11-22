"""One deterministic financial model shared by rankings, scenarios and charts."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class RetryEconomics:
    eligible_orders: int
    eligible_payment_value_paise: int
    merchant_fee_if_all_recovered_paise: int
    processing_cost_if_all_succeed_paise: int
    processing_cost_if_all_fail_paise: int
    loss_provision_if_all_recovered_paise: int


def paise(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def simulate(economics: RetryEconomics, full_gap_successes: float | Decimal,
             recovery_fraction: Decimal = Decimal(1), intervention_cost_paise: int = 0) -> dict:
    """Retry EVERY eligible failed order once; recovery is uniform within a segment.

    Existing attempts are sunk in both comparisons and are not charged twice.
    At zero recovery, retries still incur failure charges. Results are conditional
    scenarios, not predictions of intervention effectiveness.
    """
    if not recovery_fraction.is_finite() or not 0 <= recovery_fraction <= 1:
        raise ValueError("Recovery fraction must be between zero and one.")
    if intervention_cost_paise < 0 or any(value < 0 for value in economics.__dict__.values()):
        raise ValueError("Costs, fees, counts and value totals must be nonnegative.")
    gap = Decimal(str(full_gap_successes))
    if not gap.is_finite() or gap < 0:
        raise ValueError("Full-gap recovery must be finite and nonnegative.")
    m = economics.eligible_orders
    full = min(Decimal(m), gap)
    recovered = full * recovery_fraction
    q = recovered / Decimal(m) if m else Decimal(0)
    fee = q * economics.merchant_fee_if_all_recovered_paise
    successful_cost = q * economics.processing_cost_if_all_succeed_paise
    failed_cost = (1 - q) * economics.processing_cost_if_all_fail_paise
    loss = q * economics.loss_provision_if_all_recovered_paise
    net = fee - successful_cost - failed_cost - loss - intervention_cost_paise
    # net(q) = q * slope - all_failure_cost - intervention_cost.
    slope = (economics.merchant_fee_if_all_recovered_paise
             - economics.processing_cost_if_all_succeed_paise
             - economics.loss_provision_if_all_recovered_paise
             + economics.processing_cost_if_all_fail_paise)
    required_q = (Decimal(economics.processing_cost_if_all_fail_paise + intervention_cost_paise)
                  / Decimal(slope)) if slope > 0 and m else None
    required_alpha = required_q * m / full if required_q is not None and full else None
    breakdown = {
        "additional_merchant_fees_paise": paise(fee),
        "successful_retry_cost_paise": paise(successful_cost),
        "failed_retry_cost_paise": paise(failed_cost),
        "additional_loss_provision_paise": paise(loss),
        "intervention_cost_paise": intervention_cost_paise,
    }
    # Allocate sub-paise rounding once so the displayed breakdown reconciles exactly.
    display_net = (breakdown["additional_merchant_fees_paise"]
                   - breakdown["successful_retry_cost_paise"]
                   - breakdown["failed_retry_cost_paise"]
                   - breakdown["additional_loss_provision_paise"] - intervention_cost_paise)
    return {
        "currency": "INR", "eligible_orders_retried": m,
        "recovery_fraction": float(recovery_fraction),
        "full_gap_additional_successes": round(float(full), 4),
        "additional_successful_orders": round(float(recovered), 4),
        "additional_payment_value_paise": paise(q * economics.eligible_payment_value_paise),
        **breakdown,
        "incremental_net_contribution_paise": display_net,
        "rounding_adjustment_paise": display_net - paise(net),
        "break_even_recovery_fraction": float(required_alpha) if required_alpha is not None else None,
        "break_even_within_historical_gap": bool(required_alpha is not None and 0 <= required_alpha <= 1),
        "assumptions": [
            "One additional retry for every eligible failed order; no live payment is retried.",
            "Recovered orders have the same average fees, values and costs as eligible orders.",
            "Current merchant fees and as-of provider prices remain unchanged.",
            "Recovery fraction is a user assumption, not measured causal uplift.",
            "Loss provision is modelled; fixed overhead and capacity effects are excluded.",
        ],
    }

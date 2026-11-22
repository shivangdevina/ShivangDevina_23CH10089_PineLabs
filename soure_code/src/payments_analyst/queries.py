"""Canonical reconciliation and benchmark SQL, separate from validated AI exploration."""

VERSION_SQL = """SELECT id, config, history_start, current_start, window_end, as_of,
    is_synthetic, fingerprint FROM payments.dataset_versions WHERE id = %(dataset_id)s"""

DIAGNOSTICS_SQL = """SELECT
    (SELECT count(*) FROM payments.orders WHERE dataset_id = %(dataset_id)s) AS order_count,
    (SELECT count(*) FROM payments.order_facts WHERE dataset_id = %(dataset_id)s) AS fact_count,
    (SELECT coalesce(sum(amount_paise), 0) FROM payments.financial_events
        WHERE dataset_id = %(dataset_id)s) AS ledger_net_paise,
    (SELECT coalesce(sum(net_contribution_paise), 0) FROM payments.order_facts
        WHERE dataset_id = %(dataset_id)s) AS fact_net_paise"""

DAILY_SQL = """WITH priced AS (
    SELECT f.*,
        c.rule_count,
        (f.amount_paise * f.fee_bps + 5000) / 10000 + f.fee_fixed_paise AS potential_fee,
        (f.amount_paise * c.success_bps + 5000) / 10000 + c.success_fixed_paise AS potential_success_cost,
        c.failure_fixed_paise AS potential_failure_cost,
        (f.amount_paise * c.expected_loss_bps + 5000) / 10000 AS potential_loss,
        CASE WHEN f.amount_paise < 50000 THEN 'UNDER_500' ELSE '500_PLUS' END AS amount_band
    FROM payments.order_facts f
    JOIN payments.dataset_versions d ON d.id = f.dataset_id
    LEFT JOIN LATERAL (
        SELECT count(*) AS rule_count, min(success_bps) AS success_bps,
            min(success_fixed_paise) AS success_fixed_paise,
            min(failure_fixed_paise) AS failure_fixed_paise,
            min(expected_loss_bps) AS expected_loss_bps
        FROM payments.cost_rules r
        WHERE r.dataset_id = f.dataset_id AND r.bank = f.initial_bank
            AND r.payment_method = f.initial_payment_method AND r.currency = f.currency
            AND r.effective_from <= d.as_of AND d.as_of < r.effective_to
    ) c ON true
    WHERE f.dataset_id = %(dataset_id)s
        AND f.created_at >= d.history_start AND f.created_at < d.window_end
        AND (%(banks)s::text[] IS NULL OR f.initial_bank = ANY(%(banks)s::text[]))
        AND (%(methods)s::text[] IS NULL OR f.initial_payment_method = ANY(%(methods)s::text[]))
)
SELECT initial_bank AS bank, initial_payment_method AS payment_method, business_date,
    merchant_category, amount_band,
    count(*) AS total_orders,
    count(*) FILTER (WHERE is_complete) AS completed_orders,
    count(*) FILTER (WHERE is_complete AND final_status = 'SUCCESS') AS successful_orders,
    count(*) FILTER (WHERE NOT is_complete) AS pending_orders,
    count(*) FILTER (WHERE retry_eligible) AS eligible_orders,
    count(*) FILTER (WHERE retry_eligible AND rule_count <> 1) AS unpriced_orders,
    coalesce(sum(amount_paise) FILTER (WHERE retry_eligible), 0) AS eligible_payment_value_paise,
    coalesce(sum(potential_fee) FILTER (WHERE retry_eligible AND rule_count = 1), 0) AS retry_fee_paise,
    coalesce(sum(potential_success_cost) FILTER (WHERE retry_eligible AND rule_count = 1), 0) AS retry_success_cost_paise,
    coalesce(sum(potential_failure_cost) FILTER (WHERE retry_eligible AND rule_count = 1), 0) AS retry_failure_cost_paise,
    coalesce(sum(potential_loss) FILTER (WHERE retry_eligible AND rule_count = 1), 0) AS retry_loss_provision_paise,
    sum(net_contribution_paise) AS net_contribution_paise
FROM priced
GROUP BY initial_bank, initial_payment_method, business_date, merchant_category, amount_band
ORDER BY initial_bank, initial_payment_method, business_date, merchant_category, amount_band"""

LIST_DATASETS_SQL = """SELECT id, config, history_start, current_start, window_end, is_synthetic, fingerprint
    FROM payments.dataset_versions ORDER BY imported_at DESC LIMIT 50"""

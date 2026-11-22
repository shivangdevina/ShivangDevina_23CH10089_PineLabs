-- Additive Supabase PostgreSQL migration. Apply with the project's postgres owner.
-- These private schemas are NOT added to Supabase's exposed Data API schemas.
CREATE SCHEMA IF NOT EXISTS payments;
CREATE SCHEMA IF NOT EXISTS evaluation;
REVOKE ALL ON SCHEMA payments, evaluation FROM PUBLIC, anon, authenticated;

CREATE TABLE payments.dataset_versions (
    id uuid PRIMARY KEY,
    config jsonb NOT NULL,
    history_start timestamptz NOT NULL,
    current_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    as_of timestamptz NOT NULL,
    is_synthetic boolean NOT NULL CHECK (is_synthetic),
    fingerprint text NOT NULL CHECK (length(fingerprint) = 64),
    imported_at timestamptz NOT NULL DEFAULT now(),
    CHECK (history_start < current_start AND current_start < window_end AND window_end <= as_of)
);

CREATE TABLE payments.merchants (
    dataset_id uuid NOT NULL REFERENCES payments.dataset_versions(id),
    id uuid NOT NULL,
    name text NOT NULL,
    category text NOT NULL CHECK (category IN ('RETAIL', 'DIGITAL')),
    fee_bps integer NOT NULL CHECK (fee_bps BETWEEN 0 AND 10000),
    fee_fixed_paise bigint NOT NULL CHECK (fee_fixed_paise >= 0),
    PRIMARY KEY (dataset_id, id)
);

CREATE TABLE payments.cost_rules (
    dataset_id uuid NOT NULL REFERENCES payments.dataset_versions(id),
    id uuid NOT NULL,
    bank text NOT NULL,
    payment_method text NOT NULL CHECK (payment_method IN ('UPI', 'CARD')),
    effective_from timestamptz NOT NULL,
    effective_to timestamptz NOT NULL,
    success_bps integer NOT NULL CHECK (success_bps BETWEEN 0 AND 10000),
    success_fixed_paise bigint NOT NULL CHECK (success_fixed_paise >= 0),
    failure_fixed_paise bigint NOT NULL CHECK (failure_fixed_paise >= 0),
    expected_loss_bps integer NOT NULL CHECK (expected_loss_bps BETWEEN 0 AND 10000),
    currency text NOT NULL CHECK (currency = 'INR'),
    PRIMARY KEY (dataset_id, id),
    UNIQUE (dataset_id, bank, payment_method, effective_from),
    CHECK (effective_from < effective_to)
);

CREATE TABLE payments.orders (
    dataset_id uuid NOT NULL REFERENCES payments.dataset_versions(id),
    id uuid NOT NULL,
    merchant_id uuid NOT NULL,
    amount_paise bigint NOT NULL CHECK (amount_paise > 0),
    currency text NOT NULL CHECK (currency = 'INR'),
    created_at timestamptz NOT NULL,
    initial_bank text NOT NULL,
    initial_payment_method text NOT NULL CHECK (initial_payment_method IN ('UPI', 'CARD')),
    final_status text NOT NULL CHECK (final_status IN ('SUCCESS', 'FAILED', 'PENDING')),
    finalized_at timestamptz,
    PRIMARY KEY (dataset_id, id),
    FOREIGN KEY (dataset_id, merchant_id) REFERENCES payments.merchants(dataset_id, id),
    CHECK ((final_status = 'PENDING' AND finalized_at IS NULL)
        OR (final_status <> 'PENDING' AND finalized_at IS NOT NULL AND finalized_at >= created_at))
);

CREATE TABLE payments.payment_attempts (
    dataset_id uuid NOT NULL,
    id uuid NOT NULL,
    order_id uuid NOT NULL,
    cost_rule_id uuid NOT NULL,
    attempt_number integer NOT NULL CHECK (attempt_number > 0),
    bank text NOT NULL,
    payment_method text NOT NULL CHECK (payment_method IN ('UPI', 'CARD')),
    status text NOT NULL CHECK (status IN ('SUCCESS', 'FAILED', 'PENDING')),
    failure_code text,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (dataset_id, id),
    UNIQUE (dataset_id, order_id, attempt_number),
    UNIQUE (dataset_id, order_id, id),
    FOREIGN KEY (dataset_id, order_id) REFERENCES payments.orders(dataset_id, id),
    FOREIGN KEY (dataset_id, cost_rule_id) REFERENCES payments.cost_rules(dataset_id, id),
    CHECK ((status = 'FAILED' AND failure_code IS NOT NULL AND failure_code IN
        ('PROVIDER_TIMEOUT', 'NETWORK_ERROR', 'INSUFFICIENT_FUNDS', 'DO_NOT_HONOR'))
        OR (status <> 'FAILED' AND failure_code IS NULL))
);

CREATE UNIQUE INDEX one_successful_attempt_per_order
    ON payments.payment_attempts(dataset_id, order_id) WHERE status = 'SUCCESS';

CREATE TABLE payments.financial_events (
    dataset_id uuid NOT NULL,
    id uuid NOT NULL,
    order_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('MERCHANT_FEE', 'PROCESSING_COST', 'LOSS_PROVISION')),
    amount_paise bigint NOT NULL,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (dataset_id, id),
    UNIQUE (dataset_id, attempt_id, event_type),
    FOREIGN KEY (dataset_id, order_id, attempt_id)
        REFERENCES payments.payment_attempts(dataset_id, order_id, id),
    CHECK ((event_type = 'MERCHANT_FEE' AND amount_paise >= 0)
        OR (event_type <> 'MERCHANT_FEE' AND amount_paise <= 0))
);

CREATE TABLE evaluation.ground_truth (
    dataset_id uuid NOT NULL REFERENCES payments.dataset_versions(id),
    bank text NOT NULL,
    payment_method text NOT NULL,
    kind text NOT NULL,
    description text NOT NULL,
    PRIMARY KEY (dataset_id, bank, payment_method, kind)
);

CREATE TABLE payments.analysis_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES auth.users(id),
    dataset_id uuid NOT NULL REFERENCES payments.dataset_versions(id),
    question text NOT NULL,
    status text NOT NULL CHECK (status IN ('RECEIVED', 'VALIDATING', 'QUERYING', 'CALCULATING',
        'COMPLETED', 'NEEDS_CLARIFICATION', 'BLOCKED', 'FAILED')),
    analysis_plan jsonb,
    query_evidence jsonb,
    result jsonb,
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    calculation_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE payments.scenarios (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id uuid NOT NULL REFERENCES payments.analysis_runs(id),
    owner_id uuid NOT NULL REFERENCES auth.users(id),
    inputs jsonb NOT NULL,
    result jsonb NOT NULL,
    calculation_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX orders_cohort_window ON payments.orders
    (dataset_id, created_at, initial_bank, initial_payment_method);
CREATE INDEX financial_events_by_order ON payments.financial_events(dataset_id, order_id);

-- Aggregate each child independently BEFORE joining, to avoid ledger/attempt fan-out.
CREATE VIEW payments.order_facts WITH (security_invoker = true) AS
WITH attempt_totals AS (
    SELECT dataset_id, order_id, count(*) AS attempt_count
    FROM payments.payment_attempts GROUP BY dataset_id, order_id
), ledger_totals AS (
    SELECT dataset_id, order_id,
        coalesce(sum(amount_paise) FILTER (WHERE event_type = 'MERCHANT_FEE'), 0) AS merchant_fee_paise,
        -coalesce(sum(amount_paise) FILTER (WHERE event_type = 'PROCESSING_COST'), 0) AS processing_cost_paise,
        -coalesce(sum(amount_paise) FILTER (WHERE event_type = 'LOSS_PROVISION'), 0) AS loss_provision_paise,
        sum(amount_paise) AS net_contribution_paise
    FROM payments.financial_events GROUP BY dataset_id, order_id
)
SELECT o.*, m.category AS merchant_category, m.fee_bps, m.fee_fixed_paise,
    a.attempt_count, last_attempt.failure_code AS last_failure_code,
    (o.created_at AT TIME ZONE 'Asia/Kolkata')::date AS business_date,
    (o.final_status <> 'PENDING' AND o.finalized_at <= d.as_of) AS is_complete,
    (o.final_status = 'FAILED' AND o.finalized_at <= d.as_of
        AND last_attempt.failure_code IN ('PROVIDER_TIMEOUT', 'NETWORK_ERROR')) AS retry_eligible,
    coalesce(l.merchant_fee_paise, 0) AS merchant_fee_paise,
    coalesce(l.processing_cost_paise, 0) AS processing_cost_paise,
    coalesce(l.loss_provision_paise, 0) AS loss_provision_paise,
    coalesce(l.net_contribution_paise, 0) AS net_contribution_paise
FROM payments.orders o
JOIN payments.dataset_versions d ON d.id = o.dataset_id
JOIN payments.merchants m ON m.dataset_id = o.dataset_id AND m.id = o.merchant_id
JOIN attempt_totals a ON a.dataset_id = o.dataset_id AND a.order_id = o.id
LEFT JOIN ledger_totals l ON l.dataset_id = o.dataset_id AND l.order_id = o.id
JOIN LATERAL (
    SELECT p.failure_code FROM payments.payment_attempts p
    WHERE p.dataset_id = o.dataset_id AND p.order_id = o.id
    ORDER BY p.attempt_number DESC LIMIT 1
) last_attempt ON true;

-- Deny browser access by default. A later API step will add scoped backend grants.
ALTER TABLE payments.dataset_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.merchants ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.cost_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.payment_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.financial_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.analysis_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.scenarios ENABLE ROW LEVEL SECURITY;
ALTER TABLE evaluation.ground_truth ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA payments, evaluation FROM PUBLIC, anon, authenticated;

COMMENT ON VIEW payments.order_facts IS
    'Synthetic, one row per order. Net contribution includes modelled loss provision; not total company profit.';

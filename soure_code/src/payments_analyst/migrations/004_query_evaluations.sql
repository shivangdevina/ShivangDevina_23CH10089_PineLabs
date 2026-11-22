-- Public benchmark evidence contains synthetic aggregates only; no provider keys.
CREATE TABLE payments.query_evaluation_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id uuid NOT NULL REFERENCES payments.dataset_versions(id),
    model text NOT NULL,
    prompt_hash text NOT NULL CHECK (length(prompt_hash) = 64),
    suite_version text NOT NULL,
    validator_version text NOT NULL,
    dataset_fingerprint text NOT NULL CHECK (length(dataset_fingerprint) = 64),
    status text NOT NULL CHECK (status IN ('PASS', 'FAIL')),
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX query_evaluation_latest ON payments.query_evaluation_runs(dataset_id, created_at DESC);
ALTER TABLE payments.query_evaluation_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON payments.query_evaluation_runs FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON payments.query_evaluation_runs TO payment_analyst_runtime;
CREATE POLICY public_evaluation_select ON payments.query_evaluation_runs FOR SELECT TO payment_analyst_runtime
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY public_evaluation_insert ON payments.query_evaluation_runs FOR INSERT TO payment_analyst_runtime
    WITH CHECK (current_setting('app.public_workspace', true) = 'true');

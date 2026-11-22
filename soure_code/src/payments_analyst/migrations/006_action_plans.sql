-- Only the outer orchestrator persists plans; research keeps SELECT-only access.
CREATE TABLE payments.action_plans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id uuid NOT NULL,
    dataset_id uuid NOT NULL REFERENCES payments.dataset_versions(id),
    is_public boolean NOT NULL DEFAULT true CHECK (is_public),
    calculation_version text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (analysis_id,is_public) REFERENCES payments.analysis_runs(id,is_public)
);
CREATE TABLE payments.action_simulations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_plan_id uuid NOT NULL REFERENCES payments.action_plans(id),
    inputs jsonb NOT NULL,
    result jsonb NOT NULL,
    calculation_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE payments.action_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.action_simulations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON payments.action_plans,payments.action_simulations FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT ON payments.action_plans,payments.action_simulations TO payment_analyst_runtime;
CREATE POLICY public_action_plan_read ON payments.action_plans FOR SELECT TO payment_analyst_runtime
    USING (current_setting('app.public_workspace',true)='true');
CREATE POLICY public_action_plan_insert ON payments.action_plans FOR INSERT TO payment_analyst_runtime
    WITH CHECK (current_setting('app.public_workspace',true)='true');
CREATE POLICY public_action_sim_read ON payments.action_simulations FOR SELECT TO payment_analyst_runtime
    USING (current_setting('app.public_workspace',true)='true');
CREATE POLICY public_action_sim_insert ON payments.action_simulations FOR INSERT TO payment_analyst_runtime
    WITH CHECK (current_setting('app.public_workspace',true)='true');

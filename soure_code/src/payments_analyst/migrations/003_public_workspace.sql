-- Public demo access is mediated by the server's restricted runtime connection.
-- No browser/anon grants, and existing private history remains private.
ALTER TABLE payments.analysis_runs ALTER COLUMN owner_id DROP NOT NULL;
ALTER TABLE payments.scenarios ALTER COLUMN owner_id DROP NOT NULL;
ALTER TABLE payments.analysis_runs ADD COLUMN is_public boolean NOT NULL DEFAULT false;
ALTER TABLE payments.scenarios ADD COLUMN is_public boolean NOT NULL DEFAULT false;
ALTER TABLE payments.analysis_runs ADD CONSTRAINT analysis_public_owner
    CHECK ((is_public AND owner_id IS NULL) OR (NOT is_public AND owner_id IS NOT NULL));
ALTER TABLE payments.scenarios ADD CONSTRAINT scenario_public_owner
    CHECK ((is_public AND owner_id IS NULL) OR (NOT is_public AND owner_id IS NOT NULL));
ALTER TABLE payments.analysis_runs ADD CONSTRAINT analysis_public_pair UNIQUE (id, is_public);
ALTER TABLE payments.scenarios ADD CONSTRAINT scenario_analysis_visibility
    FOREIGN KEY (analysis_id, is_public) REFERENCES payments.analysis_runs(id, is_public);

CREATE POLICY public_runtime_datasets ON payments.dataset_versions FOR SELECT TO payment_analyst_runtime
    USING (current_setting('app.public_workspace', true) = 'true' AND is_synthetic);
CREATE POLICY public_runtime_merchants ON payments.merchants FOR SELECT TO payment_analyst_runtime
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY public_runtime_costs ON payments.cost_rules FOR SELECT TO payment_analyst_runtime
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY public_runtime_orders ON payments.orders FOR SELECT TO payment_analyst_runtime
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY public_runtime_attempts ON payments.payment_attempts FOR SELECT TO payment_analyst_runtime
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY public_runtime_events ON payments.financial_events FOR SELECT TO payment_analyst_runtime
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY public_analysis_select ON payments.analysis_runs FOR SELECT TO payment_analyst_runtime
    USING (is_public AND current_setting('app.public_workspace', true) = 'true');
CREATE POLICY public_analysis_insert ON payments.analysis_runs FOR INSERT TO payment_analyst_runtime
    WITH CHECK (is_public AND owner_id IS NULL AND current_setting('app.public_workspace', true) = 'true');
CREATE POLICY public_scenario_select ON payments.scenarios FOR SELECT TO payment_analyst_runtime
    USING (is_public AND current_setting('app.public_workspace', true) = 'true');
CREATE POLICY public_scenario_insert ON payments.scenarios FOR INSERT TO payment_analyst_runtime
    WITH CHECK (is_public AND owner_id IS NULL AND current_setting('app.public_workspace', true) = 'true');

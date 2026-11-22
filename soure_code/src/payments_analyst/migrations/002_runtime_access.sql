-- Runtime has source SELECT and owned-history writes; never source writes or evaluation access.
-- NOLOGIN until the owner explicitly runs configure-runtime to set a generated password.
CREATE ROLE payment_analyst_runtime NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT USAGE ON SCHEMA payments, auth TO payment_analyst_runtime;
GRANT EXECUTE ON FUNCTION auth.uid() TO payment_analyst_runtime;
GRANT SELECT ON payments.dataset_versions, payments.merchants, payments.cost_rules,
    payments.orders, payments.payment_attempts, payments.financial_events, payments.order_facts
    TO payment_analyst_runtime;
GRANT SELECT, INSERT ON payments.analysis_runs, payments.scenarios TO payment_analyst_runtime;

-- Shared, synthetic demo workspace. Every source query still requires verified identity.
CREATE POLICY runtime_read_datasets ON payments.dataset_versions FOR SELECT TO payment_analyst_runtime USING (auth.uid() IS NOT NULL);
CREATE POLICY runtime_read_merchants ON payments.merchants FOR SELECT TO payment_analyst_runtime USING (auth.uid() IS NOT NULL);
CREATE POLICY runtime_read_costs ON payments.cost_rules FOR SELECT TO payment_analyst_runtime USING (auth.uid() IS NOT NULL);
CREATE POLICY runtime_read_orders ON payments.orders FOR SELECT TO payment_analyst_runtime USING (auth.uid() IS NOT NULL);
CREATE POLICY runtime_read_attempts ON payments.payment_attempts FOR SELECT TO payment_analyst_runtime USING (auth.uid() IS NOT NULL);
CREATE POLICY runtime_read_events ON payments.financial_events FOR SELECT TO payment_analyst_runtime USING (auth.uid() IS NOT NULL);

CREATE POLICY owned_analysis_select ON payments.analysis_runs FOR SELECT TO payment_analyst_runtime USING (owner_id = auth.uid());
CREATE POLICY owned_analysis_insert ON payments.analysis_runs FOR INSERT TO payment_analyst_runtime WITH CHECK (owner_id = auth.uid());
CREATE POLICY owned_scenario_select ON payments.scenarios FOR SELECT TO payment_analyst_runtime USING (owner_id = auth.uid());
CREATE POLICY owned_scenario_insert ON payments.scenarios FOR INSERT TO payment_analyst_runtime WITH CHECK (owner_id = auth.uid());

ALTER TABLE payments.analysis_runs ADD CONSTRAINT analysis_owner_pair UNIQUE (id, owner_id);
ALTER TABLE payments.scenarios ADD CONSTRAINT scenario_analysis_owner
    FOREIGN KEY (analysis_id, owner_id) REFERENCES payments.analysis_runs(id, owner_id);
ALTER ROLE payment_analyst_runtime SET statement_timeout = '5s';
ALTER ROLE payment_analyst_runtime SET lock_timeout = '2s';

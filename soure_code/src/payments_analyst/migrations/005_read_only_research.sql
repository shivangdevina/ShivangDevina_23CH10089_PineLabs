-- The research agent has its own SELECT-only role, with no history/private access.
CREATE ROLE payment_analyst_research NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT USAGE ON SCHEMA payments TO payment_analyst_research;
GRANT SELECT ON payments.dataset_versions, payments.merchants, payments.cost_rules,
    payments.orders, payments.payment_attempts, payments.financial_events, payments.order_facts
    TO payment_analyst_research;
GRANT payment_analyst_research TO payment_analyst_runtime;
CREATE POLICY research_datasets ON payments.dataset_versions FOR SELECT TO payment_analyst_research
    USING (current_setting('app.public_workspace', true) = 'true' AND is_synthetic);
CREATE POLICY research_merchants ON payments.merchants FOR SELECT TO payment_analyst_research
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY research_costs ON payments.cost_rules FOR SELECT TO payment_analyst_research
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY research_orders ON payments.orders FOR SELECT TO payment_analyst_research
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY research_attempts ON payments.payment_attempts FOR SELECT TO payment_analyst_research
    USING (current_setting('app.public_workspace', true) = 'true');
CREATE POLICY research_events ON payments.financial_events FOR SELECT TO payment_analyst_research
    USING (current_setting('app.public_workspace', true) = 'true');

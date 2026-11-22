-- Supabase Auth plus owner-managed assignments. Open access is restricted to synthetic demo data.
CREATE TABLE payments.customers (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 120)
);
CREATE TABLE payments.user_access (
 user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
 role text NOT NULL CHECK (role IN ('pine_admin','customer')),
 customer_id uuid REFERENCES payments.customers(id),
 enabled boolean NOT NULL DEFAULT true,
 CHECK ((role='pine_admin' AND customer_id IS NULL) OR (role='customer' AND customer_id IS NOT NULL))
);
CREATE TABLE payments.customer_merchants (
 customer_id uuid NOT NULL REFERENCES payments.customers(id),
 dataset_id uuid NOT NULL, merchant_id uuid NOT NULL,
 PRIMARY KEY(dataset_id,merchant_id),
 FOREIGN KEY(dataset_id,merchant_id) REFERENCES payments.merchants(dataset_id,id)
);
CREATE INDEX customer_merchant_scope ON payments.customer_merchants(customer_id,dataset_id,merchant_id);
CREATE INDEX orders_merchant_scope ON payments.orders(dataset_id,merchant_id,id);
ALTER TABLE payments.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.user_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments.customer_merchants ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON payments.customers,payments.user_access,payments.customer_merchants FROM PUBLIC,anon,authenticated,payment_analyst_runtime,payment_analyst_research;

-- Fixed-search-path helpers inspect server-managed membership, never user_metadata/JWT role claims.
CREATE FUNCTION app_private.is_pine_admin() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM payments.user_access WHERE user_id=auth.uid() AND enabled AND role='pine_admin')
$$;
CREATE FUNCTION app_private.data_customer() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT CASE WHEN role='pine_admin' THEN nullif(current_setting('app.data_customer_id',true),'')::uuid ELSE customer_id END
 FROM payments.user_access WHERE user_id=auth.uid() AND enabled
$$;
CREATE FUNCTION app_private.public_demo(d uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT auth.uid() IS NULL AND current_setting('app.public_workspace',true)='true' AND EXISTS(
 SELECT 1 FROM payments.dataset_versions v WHERE v.id=d AND v.is_synthetic)
$$;
CREATE FUNCTION app_private.can_read_merchant(d uuid,m uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT app_private.public_demo(d) OR (app_private.is_pine_admin() AND app_private.data_customer() IS NULL) OR EXISTS(
 SELECT 1 FROM payments.customer_merchants cm WHERE cm.dataset_id=d AND cm.merchant_id=m
 AND cm.customer_id=app_private.data_customer())
$$;
CREATE FUNCTION app_private.can_read_dataset(d uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT app_private.public_demo(d) OR (app_private.is_pine_admin() AND app_private.data_customer() IS NULL) OR EXISTS(
 SELECT 1 FROM payments.customer_merchants cm WHERE cm.dataset_id=d AND cm.customer_id=app_private.data_customer())
$$;
CREATE FUNCTION app_private.access_profile() RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('user_id',a.user_id,'role',a.role,'customer_id',a.customer_id,
 'customer_name',c.name,'scope_label',CASE WHEN a.role='pine_admin' THEN 'Pine Labs · All customers' ELSE c.name END,
 'merchant_count',(SELECT count(*) FROM payments.customer_merchants cm WHERE cm.customer_id=a.customer_id))
 FROM payments.user_access a LEFT JOIN payments.customers c ON c.id=a.customer_id WHERE a.user_id=auth.uid() AND a.enabled
$$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_private FROM PUBLIC,anon,authenticated;
GRANT USAGE ON SCHEMA app_private,auth TO payment_analyst_runtime,payment_analyst_research;
GRANT EXECUTE ON FUNCTION auth.uid() TO payment_analyst_research;
GRANT EXECUTE ON FUNCTION app_private.is_pine_admin(),app_private.data_customer(),app_private.can_read_merchant(uuid,uuid),app_private.can_read_dataset(uuid),app_private.public_demo(uuid) TO payment_analyst_runtime,payment_analyst_research;
GRANT EXECUTE ON FUNCTION app_private.access_profile() TO payment_analyst_runtime;

-- Replace permissive legacy policies with explicit merchant scope and synthetic-only demo access.
DO $$ DECLARE p record; BEGIN
 FOR p IN SELECT schemaname,tablename,policyname FROM pg_policies WHERE schemaname='payments' LOOP
 EXECUTE format('DROP POLICY %I ON %I.%I',p.policyname,p.schemaname,p.tablename);
 END LOOP;
END $$;
CREATE POLICY scoped_datasets ON payments.dataset_versions FOR SELECT TO payment_analyst_runtime,payment_analyst_research USING(app_private.can_read_dataset(id));
CREATE POLICY scoped_merchants ON payments.merchants FOR SELECT TO payment_analyst_runtime,payment_analyst_research USING(app_private.can_read_merchant(dataset_id,id));
CREATE POLICY scoped_costs ON payments.cost_rules FOR SELECT TO payment_analyst_runtime,payment_analyst_research USING(app_private.can_read_dataset(dataset_id));
CREATE POLICY scoped_orders ON payments.orders FOR SELECT TO payment_analyst_runtime,payment_analyst_research USING(app_private.can_read_merchant(dataset_id,merchant_id));
CREATE POLICY scoped_attempts ON payments.payment_attempts FOR SELECT TO payment_analyst_runtime,payment_analyst_research USING(EXISTS(SELECT 1 FROM payments.orders o WHERE o.dataset_id=payment_attempts.dataset_id AND o.id=payment_attempts.order_id));
CREATE POLICY scoped_events ON payments.financial_events FOR SELECT TO payment_analyst_runtime,payment_analyst_research USING(EXISTS(SELECT 1 FROM payments.orders o WHERE o.dataset_id=financial_events.dataset_id AND o.id=financial_events.order_id));

-- Snapshot the customer scope of saved results. Old global/public reports remain admin-only.
ALTER TABLE payments.analysis_runs ADD COLUMN customer_id uuid REFERENCES payments.customers(id);
ALTER TABLE payments.scenarios ADD COLUMN customer_id uuid REFERENCES payments.customers(id);
ALTER TABLE payments.scenarios DROP CONSTRAINT scenario_analysis_owner;
ALTER TABLE payments.scenarios DROP CONSTRAINT scenario_analysis_visibility;
ALTER TABLE payments.action_plans DROP CONSTRAINT action_plans_is_public_check;
ALTER TABLE payments.action_plans DROP CONSTRAINT action_plans_analysis_id_is_public_fkey;
ALTER TABLE payments.action_plans ADD FOREIGN KEY(analysis_id) REFERENCES payments.analysis_runs(id);
ALTER TABLE payments.action_plans ADD COLUMN owner_id uuid REFERENCES auth.users(id);
ALTER TABLE payments.action_plans ADD COLUMN customer_id uuid REFERENCES payments.customers(id);
ALTER TABLE payments.action_plans ALTER COLUMN is_public SET DEFAULT false;
CREATE POLICY analysis_read ON payments.analysis_runs FOR SELECT TO payment_analyst_runtime USING(
 (is_public AND app_private.public_demo(dataset_id)) OR app_private.is_pine_admin() OR (NOT is_public AND owner_id=auth.uid() AND customer_id=app_private.data_customer()));
CREATE POLICY analysis_write ON payments.analysis_runs FOR INSERT TO payment_analyst_runtime WITH CHECK(
 (is_public AND owner_id IS NULL AND customer_id IS NULL AND app_private.public_demo(dataset_id)) OR (NOT is_public AND owner_id=auth.uid() AND app_private.can_read_dataset(dataset_id) AND customer_id IS NOT DISTINCT FROM app_private.data_customer()));
CREATE POLICY scenario_read ON payments.scenarios FOR SELECT TO payment_analyst_runtime USING(EXISTS(SELECT 1 FROM payments.analysis_runs a WHERE a.id=scenarios.analysis_id));
CREATE POLICY scenario_write ON payments.scenarios FOR INSERT TO payment_analyst_runtime WITH CHECK(
 EXISTS(SELECT 1 FROM payments.analysis_runs a WHERE a.id=scenarios.analysis_id
 AND a.customer_id IS NOT DISTINCT FROM scenarios.customer_id
 AND ((scenarios.is_public AND scenarios.owner_id IS NULL AND a.is_public AND app_private.public_demo(a.dataset_id))
 OR (NOT scenarios.is_public AND scenarios.owner_id=auth.uid()))));
CREATE POLICY plan_read ON payments.action_plans FOR SELECT TO payment_analyst_runtime USING(EXISTS(SELECT 1 FROM payments.analysis_runs a WHERE a.id=action_plans.analysis_id));
CREATE POLICY plan_write ON payments.action_plans FOR INSERT TO payment_analyst_runtime WITH CHECK(
 EXISTS(SELECT 1 FROM payments.analysis_runs a WHERE a.id=action_plans.analysis_id
 AND a.customer_id IS NOT DISTINCT FROM action_plans.customer_id AND a.dataset_id=action_plans.dataset_id
 AND ((action_plans.is_public AND action_plans.owner_id IS NULL AND a.is_public AND app_private.public_demo(a.dataset_id))
 OR (NOT action_plans.is_public AND action_plans.owner_id=auth.uid()))));
CREATE POLICY action_sim_read ON payments.action_simulations FOR SELECT TO payment_analyst_runtime USING(EXISTS(SELECT 1 FROM payments.action_plans p WHERE p.id=action_simulations.action_plan_id));
CREATE POLICY action_sim_write ON payments.action_simulations FOR INSERT TO payment_analyst_runtime WITH CHECK(EXISTS(SELECT 1 FROM payments.action_plans p WHERE p.id=action_simulations.action_plan_id));
CREATE POLICY evaluation_read ON payments.query_evaluation_runs FOR SELECT TO payment_analyst_runtime USING(app_private.is_pine_admin() OR app_private.public_demo(dataset_id));
CREATE POLICY evaluation_write ON payments.query_evaluation_runs FOR INSERT TO payment_analyst_runtime WITH CHECK(app_private.is_pine_admin() OR app_private.public_demo(dataset_id));

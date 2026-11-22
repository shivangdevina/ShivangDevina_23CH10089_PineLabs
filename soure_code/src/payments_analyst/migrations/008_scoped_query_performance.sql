-- Evaluate the small visible-merchant set once per query, rather than membership per order/event.
-- This preserves the same permissions without raising the five-second query limit.
CREATE FUNCTION app_private.visible_merchants() RETURNS TABLE(dataset_id uuid,merchant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT m.dataset_id,m.id FROM payments.merchants m
 WHERE app_private.can_read_merchant(m.dataset_id,m.id)
$$;
REVOKE ALL ON FUNCTION app_private.visible_merchants() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION app_private.visible_merchants() TO payment_analyst_runtime,payment_analyst_research;
ALTER POLICY scoped_datasets ON payments.dataset_versions USING(id IN (SELECT v.dataset_id FROM app_private.visible_merchants() v));
ALTER POLICY scoped_merchants ON payments.merchants USING((dataset_id,id) IN (SELECT v.dataset_id,v.merchant_id FROM app_private.visible_merchants() v));
ALTER POLICY scoped_costs ON payments.cost_rules USING(dataset_id IN (SELECT v.dataset_id FROM app_private.visible_merchants() v));
ALTER POLICY scoped_orders ON payments.orders USING((dataset_id,merchant_id) IN (SELECT v.dataset_id,v.merchant_id FROM app_private.visible_merchants() v));
ALTER POLICY scoped_attempts ON payments.payment_attempts USING((dataset_id,order_id) IN (SELECT o.dataset_id,o.id FROM payments.orders o));
ALTER POLICY scoped_events ON payments.financial_events USING((dataset_id,order_id) IN (SELECT o.dataset_id,o.id FROM payments.orders o));

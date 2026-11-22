"""Owner-operated provisioning only; no public endpoint can assign roles."""

from uuid import UUID
from .database import connect


def list_merchants():
    with connect() as connection:
        rows = connection.execute("SELECT dataset_id,id,name FROM payments.merchants ORDER BY dataset_id,name").fetchall()
    return [{"dataset_id": str(d), "merchant_id": str(m), "name": n} for d, m, n in rows]


def revoke_access(user_id):
    with connect() as connection:
        row = connection.execute("UPDATE payments.user_access SET enabled=false WHERE user_id=%s RETURNING user_id",
                                 (UUID(str(user_id)),)).fetchone()
    if not row:
        raise ValueError("No assignment exists for this account.")
    return {"status": "disabled", "user_id": str(user_id)}


def grant_access(user_id, role, *, customer_name=None, dataset_id=None, merchant_ids=()):
    user_id = UUID(str(user_id))
    merchant_ids = sorted(set(UUID(str(m)) for m in merchant_ids))
    if role not in {"pine_admin", "customer"}:
        raise ValueError("Choose pine_admin or customer.")
    if role == "customer" and (not customer_name or not 1 <= len(customer_name.strip()) <= 120 or not dataset_id or not merchant_ids):
        raise ValueError("Customers require a name, dataset and at least one assigned merchant.")
    if role == "pine_admin" and (customer_name or dataset_id or merchant_ids):
        raise ValueError("Pine Labs admins do not have a customer restriction.")
    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(91122026)")
        if not connection.execute("SELECT 1 FROM auth.users WHERE id=%s", (user_id,)).fetchone():
            raise ValueError("Create this user in Supabase Authentication first. No email or password was created.")
        customer_id = None
        if role == "customer":
            customer_name = customer_name.strip()
            row = connection.execute("SELECT id FROM payments.customers WHERE name=%s", (customer_name,)).fetchone()
            customer_id = row[0] if row else connection.execute(
                "INSERT INTO payments.customers(name) VALUES(%s) RETURNING id", (customer_name,)).fetchone()[0]
            for merchant_id in merchant_ids:
                if not connection.execute("SELECT 1 FROM payments.merchants WHERE dataset_id=%s AND id=%s",
                                          (dataset_id, merchant_id)).fetchone():
                    raise ValueError("A merchant does not belong to that dataset. Nothing was assigned.")
                old = connection.execute("SELECT customer_id FROM payments.customer_merchants WHERE dataset_id=%s AND merchant_id=%s",
                                         (dataset_id, merchant_id)).fetchone()
                if old and old[0] != customer_id:
                    raise ValueError("That merchant already belongs to another customer. Nothing was reassigned.")
                connection.execute("INSERT INTO payments.customer_merchants(customer_id,dataset_id,merchant_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                                   (customer_id, dataset_id, merchant_id))
        connection.execute("""INSERT INTO payments.user_access(user_id,role,customer_id) VALUES(%s,%s,%s)
            ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,customer_id=excluded.customer_id,enabled=true""",
                           (user_id, role, customer_id))
    return {"status": "assigned", "user_id": str(user_id), "role": role,
            "customer_id": str(customer_id) if customer_id else None, "merchants_assigned": len(merchant_ids)}

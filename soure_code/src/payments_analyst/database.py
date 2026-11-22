"""Supabase-only, transactional setup/import. Never echo database credentials."""

from hashlib import sha256
from importlib.resources import files
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, urlunparse

from .data import Dataset, TABLES
from .validation import validate


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def load_environment() -> None:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH, override=False)


def validate_connection_url(value: str) -> str:
    if not value or "PROJECT_REF" in value or "URL_ENCODED_PASSWORD" in value:
        raise ValueError("Configure SUPABASE_DB_URL in soure_code/.env first.")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.username or not parsed.password:
        raise ValueError("Use a PostgreSQL connection URL with the database username and password.")
    if not (host.endswith(".supabase.co") or host.endswith(".pooler.supabase.com")):
        raise ValueError("Only a hosted Supabase direct or pooler hostname is supported.")
    return value


def connection_url() -> str:
    load_environment()
    return validate_connection_url(os.environ.get("SUPABASE_DB_URL", ""))


def connect():
    import psycopg

    return psycopg.connect(connection_url(), connect_timeout=15, sslmode="require",
                           application_name="payment-analyst-setup", prepare_threshold=None)


def migrate() -> list[str]:
    """Apply packaged migrations once, refusing changed already-applied migrations."""
    applied = []
    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(91122026)")
        connection.execute("SET LOCAL statement_timeout = '60s'")
        connection.execute("CREATE SCHEMA IF NOT EXISTS app_private")
        connection.execute("REVOKE ALL ON SCHEMA app_private FROM PUBLIC, anon, authenticated")
        connection.execute("""CREATE TABLE IF NOT EXISTS app_private.schema_migrations (
            name text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())""")
        for migration in sorted(files("payments_analyst").joinpath("migrations").iterdir(), key=lambda p: p.name):
            if not migration.name.endswith(".sql"):
                continue
            sql_text = migration.read_text(encoding="utf-8")
            checksum = sha256(sql_text.encode()).hexdigest()
            old = connection.execute("SELECT checksum FROM app_private.schema_migrations WHERE name = %s",
                                     (migration.name,)).fetchone()
            if old:
                if old[0] != checksum:
                    raise ValueError("An applied migration changed. Add a new migration instead.")
                continue
            connection.execute(sql_text, prepare=False)
            connection.execute("INSERT INTO app_private.schema_migrations(name, checksum) VALUES (%s, %s)",
                               (migration.name, checksum))
            applied.append(migration.name)
    return applied


def import_dataset(data: Dataset) -> dict:
    """Validate, import atomically, reconcile, and safely skip identical repeated imports."""
    import psycopg.sql as sql
    from psycopg.types.json import Jsonb

    validate(data)
    fingerprint = data.fingerprint()
    dataset_id = data.version["id"]
    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(91122026)")
        connection.execute("SET LOCAL statement_timeout = '60s'")
        old = connection.execute("SELECT fingerprint FROM payments.dataset_versions WHERE id = %s",
                                 (dataset_id,)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise ValueError("Existing dataset content differs. Refusing to overwrite it.")
            verify_import(connection, data)
            return {"status": "already_imported", "dataset_id": dataset_id}

        version = {**data.version, "config": Jsonb(data.version["config"]), "fingerprint": fingerprint}
        columns = list(version)
        connection.execute(sql.SQL("INSERT INTO payments.dataset_versions ({}) VALUES ({})").format(
            sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() for _ in columns)), list(version.values()))

        for schema, table, rows in [("payments", t, data.tables[t]) for t in TABLES] + [
                ("evaluation", "ground_truth", data.ground_truth)]:
            if not rows:
                continue
            columns = list(rows[0])
            statement = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
                sql.Identifier(schema), sql.Identifier(table),
                sql.SQL(",").join(map(sql.Identifier, columns)))
            with connection.cursor().copy(statement) as copy:
                for row in rows:
                    copy.write_row([row[column] for column in columns])
        verify_import(connection, data)
    return {"status": "imported", "dataset_id": dataset_id, "fingerprint": fingerprint}


def verify_import(connection, data: Dataset) -> None:
    from psycopg import sql

    dataset_id = data.version["id"]
    for table in TABLES:
        count = connection.execute(sql.SQL("SELECT count(*) FROM payments.{} WHERE dataset_id = %s").format(
            sql.Identifier(table)), (dataset_id,)).fetchone()[0]
        if count != len(data.tables[table]):
            raise ValueError(f"Imported row count mismatch in {table}.")
    actual = connection.execute("""SELECT count(*), sum(net_contribution_paise)
        FROM payments.order_facts WHERE dataset_id = %s""", (dataset_id,)).fetchone()
    expected_net = sum(e["amount_paise"] for e in data.tables["financial_events"])
    if actual != (len(data.tables["orders"]), expected_net):
        raise ValueError("Imported order facts do not reconcile; transaction rolled back.")


def database_status() -> dict:
    with connect() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '5s'")
        rows = connection.execute("""SELECT id, fingerprint, is_synthetic FROM payments.dataset_versions
            ORDER BY imported_at DESC""").fetchall()
    return {"database": "supabase", "datasets": [
        {"id": str(row[0]), "fingerprint": row[1], "synthetic": row[2]} for row in rows]}


def configure_runtime() -> dict:
    """Generate runtime credentials privately; never rotate an existing login implicitly."""
    import secrets
    from dotenv import set_key
    from psycopg import sql

    owner_url = connection_url()
    if os.environ.get("SUPABASE_RUNTIME_DB_URL"):
        return {"status": "already_configured", "note": "Existing runtime credentials were preserved."}
    parsed = urlparse(owner_url)
    username = "payment_analyst_runtime"
    if parsed.hostname.endswith(".pooler.supabase.com"):
        owner_name = unquote(parsed.username)
        if "." not in owner_name:
            raise ValueError("The session pooler username must include the project reference.")
        username += "." + owner_name.split(".", 1)[1]
    password = secrets.token_urlsafe(32)
    netloc = f"{quote(username, safe='.')}:{quote(password, safe='')}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    runtime_url = urlunparse(parsed._replace(netloc=netloc))
    original = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else None
    written = False
    try:
        with connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(91122026)")
            role = connection.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s",
                                      ("payment_analyst_runtime",)).fetchone()
            if not role:
                raise ValueError("Run migrations before configuring the runtime role.")
            if role[0]:
                raise ValueError("Runtime login already exists. Restore its private connection string; no password was rotated.")
            connection.execute(sql.SQL("ALTER ROLE payment_analyst_runtime LOGIN PASSWORD {}").format(sql.Literal(password)))
            set_key(str(ENV_PATH), "SUPABASE_RUNTIME_DB_URL", runtime_url)
            written = True
    except Exception:
        if written:
            if original is None:
                ENV_PATH.unlink(missing_ok=True)
            else:
                ENV_PATH.write_text(original, encoding="utf-8")
        raise
    return {"status": "configured", "credentials_saved_to": "soure_code/.env",
            "note": "Runtime password was generated privately and was not printed."}

"""Server-owned access mode and per-workflow read scope; never client-selected roles."""

from contextlib import contextmanager
from contextvars import ContextVar
import os

from .contracts import BackendUnavailable
from .database import load_environment

research_user = ContextVar("research_user", default=None)
data_customer = ContextVar("data_customer", default=None)


def access_mode():
    load_environment()
    mode = os.environ.get("DATA_ACCESS_MODE", "public_demo")
    if mode not in {"public_demo", "rbac"}:
        raise BackendUnavailable("DATA_ACCESS_MODE must be public_demo or rbac.")
    return mode


@contextmanager
def research_identity(user_id):
    token = research_user.set(user_id)
    try:
        yield
    finally:
        research_user.reset(token)


@contextmanager
def customer_scope(customer_id):
    """Replay a saved report in its original customer scope, even for global admins."""
    token = data_customer.set(customer_id)
    try:
        yield
    finally:
        data_customer.reset(token)

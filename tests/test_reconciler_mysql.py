"""The MySQL store adapter (reconciler.adapters.mysql) and the store selection in `app`.

No database: `connection_args` is pure, and the two readers touch MySQL through exactly one
method (`_Reader._query`), which these tests replace with a list of rows. That is the same
shape the rest of the tier tests in — the point of a port is that the thing behind it can be a
fake.

What is pinned here is mostly **the decisions that are invisible at the call site**: whether a
connection is encrypted, whether a missing env var names itself, and which store a given
configuration actually selects. The row-mapping tests are the cheap half.
"""

import pytest

from reconciler import app
from reconciler.adapters.mysql import MysqlMilestones, MysqlTasks, connection_args
from reconciler.adapters.neon import NeonMilestones, NeonTasks
from reconciler.core import Cycle
from reconciler.definition import GENESIS_STAGE

DSN = "mysql://bob:s3cret@db.example.com:3307/reconciler"
ENV_VARS = ("RECONCILER_STORE", "NEON_DATABASE_URL", "MYSQL_DATABASE_URL")


@pytest.fixture
def clean_env(monkeypatch):
    """No ambient store configuration — a developer's `.env` must not decide a test."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def reader(cls, rows):
    """One reader whose only database call returns `rows`."""
    instance = cls(DSN)
    instance._query = lambda sql, params: rows        # noqa: SLF001 — the seam being faked
    return instance


class Definition:
    """Just enough `ProcessDefinition` for the two queries that take one."""

    name = "dreamhost_invoice"


# --- connection_args: every decision about encryption lives here ------------------------------

def test_tls_is_on_without_being_asked_for():
    """Plaintext must never be what you get by forgetting something.

    The cluster reaches a managed MySQL VPS across the public internet carrying invoice
    amounts and email subjects, so "nobody set the flag" and "we decided not to encrypt" have
    to be distinguishable — which they are only if the default is the safe one.
    """
    assert "ssl" in connection_args(DSN)


def test_tls_can_be_disabled_but_only_by_typing_it():
    assert "ssl" not in connection_args(DSN + "?ssl=disable")


def test_a_ca_turns_encryption_into_authentication():
    """Encrypted-but-unverified and verified are different guarantees, so they are different
    configurations rather than one flag — without a CA the connection resists eavesdropping
    and not interception, and nothing should let a DSN imply otherwise."""
    bare = connection_args(DSN)
    assert bare["ssl"] == {} and not bare.get("ssl_verify_identity")

    verified = connection_args(DSN + "?ssl_ca=/etc/ssl/dreamhost.pem")
    assert verified["ssl"] == {"ca": "/etc/ssl/dreamhost.pem"}
    assert verified["ssl_verify_cert"] and verified["ssl_verify_identity"]


def test_credentials_and_target_are_parsed():
    args = connection_args(DSN)
    assert (args["host"], args["port"]) == ("db.example.com", 3307)
    assert (args["user"], args["password"]) == ("bob", "s3cret")
    assert args["database"] == "reconciler"


def test_the_port_defaults_rather_than_being_required():
    assert connection_args("mysql://u:p@host/db")["port"] == 3306


def test_a_password_with_url_syntax_in_it_survives():
    """A generated password containing `@` or `/` is URL-encoded in the DSN, and a reader that
    forgot to unquote would authenticate with the wrong string and report it as bad
    credentials — a failure that reads like a permissions problem."""
    args = connection_args("mysql://u:p%40ss%2Fword@host/db")
    assert args["password"] == "p@ss/word"


@pytest.mark.parametrize("dsn, missing", [
    ("postgres://u:p@host/db", "scheme"),
    ("mysql:///db", "host"),
    ("mysql://u:p@host", "database"),
])
def test_an_unusable_dsn_says_which_part_is_wrong(dsn, missing):
    with pytest.raises(ValueError, match=missing):
        connection_args(dsn)


# --- reading this tier's own state ------------------------------------------------------------

def test_cycles_come_back_as_cycles():
    rows = [("dh_invoice_2026_004", "rods", 1788000000, '{"invoice_number": 4}')]
    cycles = reader(MysqlMilestones, rows).cycles(Definition())

    assert len(cycles) == 1
    assert cycles[0].key == "dh_invoice_2026_004"
    assert cycles[0].process == "dreamhost_invoice"
    assert cycles[0].opened_at == 1788000000
    assert cycles[0].context == {"invoice_number": 4}


def test_context_needs_no_reserved_key_stripping():
    """The Postgres reader rebuilds `context` by deleting `id`/`name`/`cycle_key`/… from the
    genesis body, because there the context and the envelope are one row. Here it is its own
    column, so what is stored is what comes back — and a stripping pass would be a bug waiting
    for a context field that happens to be called `name`."""
    rows = [("k", "rods", 1, '{"name": "a line item", "invoice_number": 4}')]
    assert reader(MysqlMilestones, rows).cycles(Definition())[0].context == {
        "name": "a line item", "invoice_number": 4}


def test_milestones_are_keyed_by_stage_including_genesis():
    """A stage may depend on nothing and still need somewhere to look back to, so the genesis
    event is a milestone like any other rather than a special case."""
    rows = [(GENESIS_STAGE, 1788000000, '{"invoice_number": 4}'),
            ("data_approved", 1788003600, '{"matched_at": 1788003000}')]
    found = reader(MysqlMilestones, rows).milestones(Definition(), Cycle(
        key="dh_invoice_2026_004", process="dreamhost_invoice",
        user_id="rods", opened_at=1788000000))

    assert set(found) == {GENESIS_STAGE, "data_approved"}
    assert found["data_approved"].timestamp == 1788003600
    assert found["data_approved"].payload == {"matched_at": 1788003000}


@pytest.mark.parametrize("stored, expected", [
    ('{"a": 1}', {"a": 1}),                 # PyMySQL hands a JSON column back as text
    (b'{"a": 1}', {"a": 1}),                # ...or as bytes, depending on the charset
    ({"a": 1}, {"a": 1}),                   # ...or decoded, if a future driver does that
    (None, {}),                             # a column never written is empty, not a parse error
])
def test_a_json_column_is_read_whatever_the_driver_returns(stored, expected):
    rows = [("k", "rods", 1, stored)]
    assert reader(MysqlMilestones, rows).cycles(Definition())[0].context == expected


def test_the_store_cannot_read_awares_event_log():
    """`signals()` is deliberately absent, and this test is the whole point of splitting
    `core.EventLog` out of `core.MilestoneStore`.

    A signal is a fact *Aware* recorded that a process is waiting on ("you got home"); it lives
    in Neon and this store genuinely cannot see it. Implementing the method and raising would
    push that discovery to the moment a cycle is mid-flight; not having it means a process whose
    `await` names an Aware event fails to wire up instead, which is the failure this tier
    prefers — `world.NotYetImplemented` exists for exactly the same reason.
    """
    assert not hasattr(MysqlMilestones(DSN), "signals")
    assert hasattr(NeonMilestones("postgres://x/y"), "signals")


# --- reading the open task list ---------------------------------------------------------------

def test_open_tasks_come_back_keyed_by_upstream_id():
    rows = [("mail-1", "Renew car insurance", "AXA", "service@axa.ch", 1788000000)]
    found = reader(MysqlTasks, rows).open_tasks("rods", "aware/todo")

    assert set(found) == {"mail-1"}
    assert found["mail-1"].subject == "Renew car insurance"
    assert found["mail-1"].from_address == "service@axa.ch"
    assert found["mail-1"].opened_at == 1788000000


def test_a_task_with_null_text_columns_is_still_a_task():
    """A task is identified by its Gmail id, so missing prose must degrade the row rather than
    drop it — an untitled mail still has to be tickable, and `None` reaching the dashboard as a
    subject would render as the string "None"."""
    rows = [("mail-1", None, None, None, None)]
    task = reader(MysqlTasks, rows).open_tasks("rods", "aware/todo")["mail-1"]
    assert (task.subject, task.from_name, task.from_address) == ("", "", "")
    assert task.opened_at == 0


def test_openness_is_a_column_not_an_anti_join():
    """The latest-open-vs-latest-close pair of CTEs collapses to one predicate here. Pinned
    because the *reopen* semantics it protected must survive the move: re-labelling a finished
    mail has to reopen it, which on this schema is a write that clears `closed_at` (Phase 3),
    not an `IS NULL` test that can never be undone."""
    from reconciler.adapters.mysql import _OPEN_TASKS_SQL
    assert "closed_at IS NULL" in _OPEN_TASKS_SQL
    assert "DISTINCT" not in _OPEN_TASKS_SQL.upper()


# --- which store a configuration selects ------------------------------------------------------

def test_neon_is_the_default_because_cutover_follows_a_backfill(clean_env):
    """An empty MySQL reads as "nothing is open" rather than as an error, which would make a
    sweep re-open every labelled mail. So the move has to be a deliberate act taken after the
    backfill — and the rollback is the same act in reverse."""
    clean_env.setenv("NEON_DATABASE_URL", "postgres://x/y")
    assert isinstance(app.milestone_store(), NeonMilestones)
    assert isinstance(app.task_store(), NeonTasks)


def test_selecting_mysql_switches_both_stores(clean_env):
    clean_env.setenv("RECONCILER_STORE", "mysql")
    clean_env.setenv("MYSQL_DATABASE_URL", DSN)
    assert isinstance(app.milestone_store(), MysqlMilestones)
    assert isinstance(app.task_store(), MysqlTasks)


def test_a_missing_dsn_names_the_variable_the_choice_implies(clean_env):
    """The two stores read different env vars, so "not set" has to say WHICH — otherwise
    flipping the store and forgetting its DSN reports the other one's name."""
    clean_env.setenv("RECONCILER_STORE", "mysql")
    with pytest.raises(app.ConfigurationError, match="MYSQL_DATABASE_URL"):
        app.milestone_store()


def test_an_unknown_store_is_refused_rather_than_defaulted(clean_env):
    """A typo must not silently fall back to Neon: that is how a cutover appears to have
    happened while every read still goes to the database it was meant to leave."""
    clean_env.setenv("RECONCILER_STORE", "mysqll")
    clean_env.setenv("NEON_DATABASE_URL", "postgres://x/y")
    with pytest.raises(app.ConfigurationError, match="mysqll"):
        app.milestone_store()

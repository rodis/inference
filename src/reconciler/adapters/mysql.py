"""Reading recorded state back out of MySQL — the tier's own store (ADR 0012, amended).

The counterpart of `adapters.neon`, and the reason it exists is a measurement rather than a
preference: the reconciler's *reads* are the only thing that wakes Neon's compute on a clock.
A `sweep-tasks` run does ten seconds of work and buys a five-minute wake, so a `*/15` cron can
never let the compute sleep more than ten minutes at a stretch — a 33% duty cycle that holds on
a day when nothing happened at all. Everything else touching Neon is proportional to real
activity. Moving the reads here removes the clock-driven waker; the writes stay where they are.

**This reverses ADR 0012's "the tier needs no store of its own", deliberately.** What it does
NOT reverse is normative rule 1: the reconciler still acts and Aware still observes. Every
milestone is still POSTed at the ingest gateway (`adapters.gateway`), so Neon keeps a complete
copy, the Aware timeline and both dashboard boards keep working untouched — and MySQL stays
*reconstructible from the event log*, which is what keeps a store with no backup story from
being a new single point of data loss.

Reads only, exactly like `adapters.neon`. Nothing here writes.

**Two ports, not one** (see `core.MilestoneStore`): `MysqlMilestones` serves this tier's own
state, `MysqlTasks` serves the sweep. Neither implements `core.EventLog` — `signals()` reads
*Aware's* raw event log, which lives in Neon and which this store genuinely cannot see. That
absence is structural rather than an oversight: a process whose `await` names an Aware event
must keep a Neon reader, and finding out at wiring time is the point.

**Schema expected** (authored in Phase 2; this adapter is written against it):

    cycles     (cycle_key PK, process, user_id, opened_at, context JSON)
    milestones (cycle_key, stage, occurred_at, payload JSON, PRIMARY KEY (cycle_key, stage))
    tasks      (upstream_id PK, user_id, label, subject, from_name, from_address,
                opened_at, closed_at NULL, closed_via)

Two shapes are simpler here than in Postgres, and both are the schema doing work Python used
to do: `PRIMARY KEY (cycle_key, stage)` *is* the last-write-wins rule `NeonMilestones.milestones`
implements with a dict, and `WHERE closed_at IS NULL` replaces the latest-open-vs-latest-close
anti-join wholesale. The reopen semantics that anti-join protected survive as a write-side rule
(clear `closed_at`, bump `opened_at`) — see `tasks.py` for why re-labelling a finished mail has
to work.

**Until Phase 2 backfills these tables they are empty**, and an empty `tasks` table reads as
"nothing is open" rather than as an error — which would make a sweep re-open every labelled
mail. That is why the store is selected by an env var that defaults to Neon: flipping it is a
deliberate act that comes *after* the backfill, never before.

**The target, verified from inside the cluster.** MySQL **8.4.8** at `prod-mysql.rods.me`,
database `rods_reconciler`, user `rods_reconciler`, negotiating TLSv1.3 /
TLS_AES_256_GCM_SHA384. Two properties of it shape the code here rather than the deployment:

- The certificate is MySQL 8.4's **auto-generated, self-signed** one, so the CA must be pinned
  and `ssl_verify_identity` must stay off — its CN is not the hostname.
- The account uses **`caching_sha2_password`** (8.4 dropped `mysql_native_password`), which
  needs TLS to authenticate at all. TLS is a correctness requirement here, not a hardening
  option, which is why there is no plaintext path.

**Allowable Hosts is enforced before the handshake and matches on reverse DNS.** The three
current k8s worker IPs are allow-listed. The consequence worth knowing at 3am: a connection
from a new or rebuilt node is refused in a way that looks like an **auth** failure, not a
network one — so "bad credentials" after a node replacement is the allow-list, not the
password.

`pymysql` is a dependency of the *runner image* (`workers/reconciler/requirements.txt`) and
deliberately not of the project, so a local checkout does not have it — `pip install -e .`
would otherwise put a MySQL driver in the inference runtime's image too. To run this adapter
by hand, bring it along for the one command, the way the tier already borrows Prefect:

    (cd workers && uv run --with pymysql python -m reconciler.run sweep-tasks --dry-run)
"""

import json
import logging
import urllib.parse

from reconciler.core import Cycle, Milestone
from reconciler.definition import ProcessDefinition
from reconciler.tasks import OpenTask

logger = logging.getLogger("reconciler.adapters.mysql")

DEFAULT_PORT = 3306

_CYCLES_SQL = """
    SELECT cycle_key, user_id, opened_at, context
      FROM cycles
     WHERE process = %(process)s
     ORDER BY opened_at
"""

# No `process` predicate: `cycles.cycle_key` is the primary key, so it is unique across every
# process and the cycle already names exactly one.
_MILESTONES_SQL = """
    SELECT stage, occurred_at, payload
      FROM milestones
     WHERE cycle_key = %(cycle_key)s
     ORDER BY occurred_at
"""

# The whole anti-join, collapsed. A task is open when it has no close — which is a column here
# rather than a second event to out-join against.
_OPEN_TASKS_SQL = """
    SELECT upstream_id, subject, from_name, from_address, opened_at
      FROM tasks
     WHERE user_id = %(user_id)s
       AND label = %(label)s
       AND closed_at IS NULL
"""


def connection_args(dsn: str) -> dict:
    """Parse `mysql://user:pass@host:port/db` into PyMySQL's keyword arguments.

    Extracted and public so it can be asserted directly — every decision that governs whether
    this connection is encrypted is made here, and a test should be able to read it without a
    database.

    **TLS is mandatory, and not only for privacy.** MySQL 8.4 removed `mysql_native_password`
    as a built-in, so the account authenticates with `caching_sha2_password`, which requires
    TLS (or an RSA key exchange) for any login that is not already cached. Without it the
    connection fails as what reads like a bad password. `?ssl=disable` therefore raises rather
    than being honoured — it would only ever produce a misleading error.

    Two settings are not interchangeable and both are deliberate:

    - `ssl_ca` **pins** the server's self-signed certificate. Without it the connection is
      still encrypted but the peer is unauthenticated — eavesdropping is resisted,
      interception is not — and a warning says so rather than letting the DSN imply more.
    - `ssl_verify_identity` is **always False**, because the auto-generated certificate's CN
      is not the hostname (see the comment at the assignment).
    """
    parsed = urllib.parse.urlsplit(dsn)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise ValueError(f"not a mysql DSN: scheme {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("mysql DSN has no host")

    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError("mysql DSN names no database")

    query = urllib.parse.parse_qs(parsed.query)
    args: dict = {
        "host": parsed.hostname,
        "port": parsed.port or DEFAULT_PORT,
        "user": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
        "database": database,
        "charset": "utf8mb4",
        # Reads only, but an open transaction on a connection that is about to be closed is
        # noise in the server's process list and nothing else.
        "autocommit": True,
        "connect_timeout": int(query.get("connect_timeout", ["10"])[0]),
    }

    if query.get("ssl", ["require"])[0] == "disable":
        raise ValueError(
            "?ssl=disable is not supported. MySQL 8.4 removed mysql_native_password as a "
            "built-in, so this account authenticates with caching_sha2_password — which "
            "requires TLS (or an RSA key exchange) for any login that is not already cached. "
            "TLS is load-bearing for the connection to work at all, not only for privacy, and "
            "a plaintext attempt fails as what reads like a bad password. There is no useful "
            "no-TLS path to offer, so there is not one.")

    ca = query.get("ssl_ca", [None])[0]
    # `ssl={"ca": …}` pins; `ssl={}` is PyMySQL's "negotiate TLS with nothing to check it
    # against". Either way TLS is negotiated — verified TLSv1.3 / TLS_AES_256_GCM_SHA384.
    args["ssl"] = {"ca": ca} if ca else {}
    args["ssl_verify_cert"] = bool(ca)
    # NEVER True, even with a CA pinned. The server presents MySQL 8.4's auto-generated
    # certificate, whose CN is `MySQL_Server_8.4.8_Auto_Generated_Server_Certificate` rather
    # than the hostname, so an identity check cannot pass — it would reject a server we had
    # just successfully authenticated. Pinning the CA is what authenticates the peer instead,
    # the same trade the estate already makes for vmauth's self-signed certificate.
    args["ssl_verify_identity"] = False

    if not ca:
        logger.warning("TLS without ssl_ca: the connection to %s is encrypted but the server "
                       "is UNAUTHENTICATED. The certificate is self-signed, so only a pinned "
                       "CA authenticates it.", args["host"])
    return args


class _Reader:
    """A fresh connection per query, and the shared parsing both readers need."""

    def __init__(self, dsn: str):
        self._args = connection_args(dsn)

    def _query(self, sql: str, params: dict) -> list[tuple]:
        # pymysql is imported HERE, not at module scope, and that is a CI contract rather than
        # a style choice: the checks workflow installs with `pip --no-deps`, so anything a test
        # imports transitively must be importable with no third-party packages present. This
        # module is reached from `reconciler.app`, which the flow and CLI tests both import —
        # the same trap `adapters/neon.py` fell into with a module-level `import psycopg`,
        # which collapsed the whole pytest collection.
        import pymysql

        connection = pymysql.connect(**self._args)
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        finally:
            connection.close()

    @staticmethod
    def _json(value) -> dict:
        """A JSON column, whatever the driver handed back.

        PyMySQL returns a MySQL `JSON` column as text rather than decoding it, and which of
        `str`/`bytes` depends on the connection charset — so normalise here instead of at four
        call sites. `None` is a column that was never written, not a parse failure.
        """
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, bytes | bytearray):
            value = value.decode("utf-8")
        return json.loads(value)


class MysqlMilestones(_Reader):
    """Loads cycles and their milestones for a process. Satisfies `core.MilestoneStore`."""

    def cycles(self, definition: ProcessDefinition) -> list[Cycle]:
        rows = self._query(_CYCLES_SQL, {"process": definition.name})
        cycles = [
            Cycle(key=cycle_key, process=definition.name, user_id=user_id,
                  opened_at=int(opened_at), context=self._json(context))
            for cycle_key, user_id, opened_at, context in rows
        ]
        logger.info("process %s has %d cycle(s)", definition.name, len(cycles))
        return cycles

    def milestones(self, definition: ProcessDefinition,
                   cycle: Cycle) -> dict[str, Milestone]:
        rows = self._query(_MILESTONES_SQL, {"cycle_key": cycle.key})
        # No last-write-wins pass: `PRIMARY KEY (cycle_key, stage)` means the row IS the
        # resolution, so a re-emitted milestone updated its row rather than adding one.
        return {
            stage: Milestone(stage=stage, timestamp=int(occurred_at),
                             payload=self._json(payload))
            for stage, occurred_at, payload in rows
        }


class MysqlTasks(_Reader):
    """The open half of the email todo list. Satisfies `tasks.OpenTaskStore`."""

    def open_tasks(self, user_id: str, label: str) -> dict[str, OpenTask]:
        rows = self._query(_OPEN_TASKS_SQL, {"user_id": user_id, "label": label})
        found = {
            upstream_id: OpenTask(
                upstream_id=upstream_id,
                subject=subject or "",
                from_name=from_name or "",
                from_address=from_address or "",
                opened_at=int(opened_at or 0),
            )
            for upstream_id, subject, from_name, from_address, opened_at in rows
        }
        logger.info("%d open task(s) for %s", len(found), user_id)
        return found

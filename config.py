"""Load and validate config.yaml."""
import re
from dataclasses import dataclass

import yaml

PARAM_RE = re.compile(r"%\((\w+)\)s")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_rows: int = 500
    default_rows: int = 50
    statement_timeout_ms: int = 15_000


@dataclass(frozen=True)
class Instance:
    key: str
    title: str
    host: str
    port: int
    user: str
    password_env: str
    databases: tuple[str, ...]
    environment: str = ""  # badge in the UI, e.g. "Local" or "QA2"


@dataclass(frozen=True)
class SavedQuery:
    key: str
    title: str
    instance: str
    database: str
    params: tuple[str, ...]
    sql: str


@dataclass(frozen=True)
class Config:
    limits: Limits
    instances: dict[str, Instance]
    users: dict[str, str]  # username -> bcrypt hash
    saved_queries: dict[str, SavedQuery]


def load_config(path) -> Config:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f) or {})


def parse_config(raw: dict) -> Config:
    try:
        limits = Limits(**(raw.get("limits") or {}))
    except TypeError as e:
        raise ConfigError(f"limits: {e}") from e
    if not 1 <= limits.default_rows <= limits.max_rows:
        raise ConfigError("limits: need 1 <= default_rows <= max_rows")

    instances = {key: _instance(key, v) for key, v in (raw.get("instances") or {}).items()}
    if not instances:
        raise ConfigError("no instances configured")

    users = {}
    for name, v in (raw.get("users") or {}).items():
        hashed = str((v or {}).get("password_hash", ""))
        if not hashed.startswith("$2"):
            raise ConfigError(f"user {name}: password_hash must be a bcrypt hash (run hashpw.py)")
        users[name] = hashed

    saved = {key: _saved_query(key, v, instances) for key, v in (raw.get("saved_queries") or {}).items()}
    return Config(limits, instances, users, saved)


def _instance(key: str, v: dict) -> Instance:
    if not KEY_RE.match(key):
        raise ConfigError(f"instance {key}: key may only use letters, digits, '_' and '-'")
    try:
        return Instance(
            key=key, title=v["title"], host=v["host"], port=int(v["port"]), user=v["user"],
            password_env=v["password_env"], databases=tuple(v["databases"]),
            environment=str(v.get("environment") or ""),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"instance {key}: missing or bad field {e}") from e


def _saved_query(key: str, v: dict, instances: dict[str, Instance]) -> SavedQuery:
    try:
        q = SavedQuery(
            key=key, title=v["title"], instance=v["instance"], database=v["database"],
            params=tuple(v.get("params") or ()), sql=v["sql"].strip(),
        )
    except (KeyError, TypeError, AttributeError) as e:
        raise ConfigError(f"saved query {key}: missing or bad field {e}") from e
    inst = instances.get(q.instance)
    if inst is None or q.database not in inst.databases:
        raise ConfigError(f"saved query {key}: unknown instance/database {q.instance}/{q.database}")
    # The runner already wraps every query in a READ ONLY transaction; these checks
    # keep a saved query to the one reviewed read statement, so nothing like a
    # second statement or a SET can ride along.
    if ";" in q.sql:
        raise ConfigError(f"saved query {key}: must be a single statement without ';'")
    if not re.match(r"(?is)^(select|with)\b", q.sql):
        raise ConfigError(f"saved query {key}: must start with SELECT or WITH")
    used = set(PARAM_RE.findall(q.sql))
    if used != set(q.params):
        raise ConfigError(f"saved query {key}: params {sorted(q.params)} don't match placeholders {sorted(used)}")
    return q

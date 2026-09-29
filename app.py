"""DB viewer web app. Every database call goes through db.run_readonly."""
import re
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from audit import AuditLog
from auth import check_login
from builder import (AGGREGATES, JOIN_KINDS, MAX_JOINS, OPERATORS, Aggregate, Filter, Join, QueryError,
                     QueryRequest, aggregate_label, build_select)
from cells import format_cell, to_csv
from config import load_config
from db import Databases, DatabaseError, QueryTimeout, UnknownDatabase, explain, run_readonly

SESSION_MAX_AGE_SECONDS = 8 * 3600
BLANK_FILTER = {"column": "", "op": "=", "value": ""}
BLANK_JOIN = {"kind": "LEFT", "table": "", "left": "", "right": ""}
BLANK_AGGREGATE = {"func": "", "column": ""}
TABS = ("rows", "structure", "indexes")
# builder.py's alias syntax: tN.column
ALIASED_REF = re.compile(r"^t(\d)\.(.+)$", re.DOTALL)
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


class FilterIn(BaseModel):
    column: str
    op: str
    value: str = ""


class JoinIn(BaseModel):
    kind: str = "LEFT"
    schema_name: str = "public"
    table: str
    left: str
    right: str


class AggregateIn(BaseModel):
    func: str
    column: str = ""


class TableQueryIn(BaseModel):
    instance: str
    database: str
    schema_name: str
    table: str
    columns: list[str] = []
    filters: list[FilterIn] = []
    order_by: str | None = None
    descending: bool = False
    limit: int | None = None
    joins: list[JoinIn] = []
    aggregates: list[AggregateIn] = []


def _request_from_api(body: TableQueryIn) -> QueryRequest:
    return QueryRequest(
        body.schema_name, body.table, tuple(body.columns),
        tuple(Filter(f.column, f.op, f.value) for f in body.filters),
        body.order_by or None, body.descending, body.limit,
        tuple(Join(j.kind, j.schema_name, j.table, j.left, j.right) for j in body.joins),
        tuple(Aggregate(a.func, a.column) for a in body.aggregates),
    )


def _audit_fields(req: QueryRequest) -> dict:
    return {
        "table": f"{req.schema}.{req.table}", "columns": list(req.columns),
        "filters": [asdict(f) for f in req.filters], "order_by": req.order_by,
        "descending": req.descending, "limit": req.limit,
        "joins": [asdict(j) for j in req.joins], "aggregates": [asdict(a) for a in req.aggregates],
    }


def create_app(config_path, audit_path, env: Mapping[str, str]) -> FastAPI:
    config = load_config(config_path)
    secret = env.get("SESSION_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must be set to at least 32 characters")
    audit = AuditLog(audit_path)
    dbs = Databases(config, env)
    limits = config.limits

    @asynccontextmanager
    async def lifespan(_app):
        yield
        dbs.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret, max_age=SESSION_MAX_AGE_SECONDS, same_site="strict")

    def current_user(request: Request) -> str | None:
        user = request.session.get("user")
        # A user removed from config.yaml loses access on the next request.
        return user if user in config.users else None

    # --- running things (one audit line per run) ---

    def execute(user, instance, database, event, audit_fields, work):
        """Run `work` (which returns (payload, row_count)); returns (status, payload)."""
        started = time.monotonic()
        try:
            payload, rows = work()
        except (QueryError, UnknownDatabase, DatabaseError) as e:
            status, message = 400, str(e)
        except QueryTimeout:
            status, message = 408, f"Query took over {limits.statement_timeout_ms // 1000} s — add a filter on an indexed column."
        else:
            audit.write(event, user=user, instance=instance, database=database,
                        rows=rows, duration_ms=payload["duration_ms"], **audit_fields)
            return 200, payload
        audit.write(event, user=user, instance=instance, database=database, error=message,
                    duration_ms=int((time.monotonic() - started) * 1000), **audit_fields)
        return status, {"error": message}

    def rows_payload(result, params):
        return {
            "columns": result.columns,
            "rows": [[format_cell(v) for v in row] for row in result.rows],
            "duration_ms": result.duration_ms, "sql": result.sql_text, "params": params,
        }, len(result.rows)

    def run_table(user, instance, database, req: QueryRequest):
        def work():
            query, params = build_select(dbs.catalog(instance, database), req, limits.default_rows, limits.max_rows)
            return rows_payload(run_readonly(dbs.pool(instance, database), query, params, limits.statement_timeout_ms), params)
        return execute(user, instance, database, "query", _audit_fields(req), work)

    def run_explain(user, instance, database, req: QueryRequest):
        def work():
            query, params = build_select(dbs.catalog(instance, database), req, limits.default_rows, limits.max_rows)
            plan = explain(dbs.pool(instance, database), query, params, limits.statement_timeout_ms)
            return {"plan": plan.lines, "duration_ms": plan.duration_ms, "sql": plan.sql_text, "params": params}, len(plan.lines)
        return execute(user, instance, database, "explain", _audit_fields(req), work)

    def run_saved(user, key, values: Mapping[str, str]):
        saved = config.saved_queries.get(key)
        if saved is None:
            return 404, {"error": f"unknown saved query {key}"}
        params = {p: values.get(p, "") for p in saved.params}
        return execute(user, saved.instance, saved.database, "query", {"saved_query": key, "params": params},
                       lambda: rows_payload(run_readonly(dbs.pool(saved.instance, saved.database), saved.sql,
                                                         params, limits.statement_timeout_ms), params))

    def table_names(instance, database, refresh=False) -> dict[str, tuple[str, str]]:
        """'schema.table' -> (schema, table), looked up exactly (never split on '.')."""
        return {f"{s}.{t}": (s, t) for s, t in dbs.catalog(instance, database, refresh=refresh)}

    def run_table_info(user, instance, database, table_value):
        def work():
            started = time.monotonic()
            key = table_names(instance, database).get(table_value)
            if key is None:
                raise QueryError(f"unknown table {table_value}")
            info = dbs.table_info(instance, database, *key)
            return {**info, "duration_ms": int((time.monotonic() - started) * 1000)}, len(info["columns"])
        return execute(user, instance, database, "table_info", {"table": table_value}, work)

    # --- the page ---

    def browse(db_value: str, table_value: str, refresh: bool = False) -> dict:
        """Resolve the selected database and table for the page."""
        instance, _, database = db_value.partition("/")
        ctx = {"sel": {"instance": instance, "database": database, "table": table_value},
               "table_key": None, "names": {}, "catalog": {}}
        if not db_value:
            return ctx
        try:
            catalog = dbs.catalog(instance, database, refresh=refresh)
        except (UnknownDatabase, DatabaseError, QueryTimeout) as e:
            ctx["browse_error"] = str(e)
            return ctx
        names = {f"{schema}.{table}": (schema, table) for schema, table in catalog}
        ctx.update(tables=sorted(names), names=names, catalog=catalog, table_key=names.get(table_value))
        return ctx

    def query_tables(ctx: dict, joins: list[dict]) -> list[dict]:
        """Base table plus one entry per join, with aliases and output labels.

        A join naming an unknown table keeps its slot (with no columns), so later
        aliases and form rows stay aligned; running it reports the unknown table.
        """
        tables = []
        keys = [ctx["table_key"]] + [ctx["names"].get(j["table"]) for j in joins]
        names = [None] + [j["table"] for j in joins]
        for key, name in zip(keys, names):
            if key is None:
                tables.append({"alias": f"t{len(tables)}", "schema": "", "table": name, "label": name,
                               "columns": [], "missing": True})
                continue
            seen = any(t["table"] == key[1] for t in tables)
            tables.append({"alias": f"t{len(tables)}", "schema": key[0], "table": key[1],
                           "label": f"{key[1]}#{len(tables)}" if seen else key[1],
                           "columns": ctx["catalog"][key]})
        return tables

    def drop_alias(state: dict, removed: int) -> None:
        """After removing table tN from the query: drop refs to it and renumber later aliases."""
        def shift(ref: str) -> str | None:
            match = ALIASED_REF.match(ref)
            if not match:
                return ref
            index = int(match[1])
            if index == removed:
                return None
            return f"t{index - 1}.{match[2]}" if index > removed else ref

        state["columns"] = [c for c in map(shift, state["columns"]) if c]
        state["filters"] = [dict(f, column=shift(f["column"])) for f in state["filters"] if shift(f["column"])]
        state["aggregates"] = [dict(a, column=shift(a["column"]) if a["column"] else "")
                               for a in state["aggregates"] if not a["column"] or shift(a["column"])]
        state["order_by"] = shift(state["order_by"]) or ""
        for join in state["joins"]:
            join["left"] = shift(join["left"]) or ""  # pointed at the removed table: make the user choose again

    def normalize_ref(ref: str) -> str:
        return ref if ref[:1] == "t" and ref[1:2].isdigit() and ref[2:3] == "." else f"t0.{ref}"

    def read_state(form) -> dict:
        """The builder form as submitted: joins, aggregates, filters, columns, sort, limit."""
        joins = [
            {"kind": str(k) or "LEFT", "table": str(t), "left": str(lft), "right": str(r)}
            for k, t, lft, r in zip(form.getlist("join_kind"), form.getlist("join_table"),
                                    form.getlist("join_left"), form.getlist("join_right"))
            if t
        ]
        aggregates = [{"func": str(f), "column": str(c)}
                      for f, c in zip(form.getlist("agg_func"), form.getlist("agg_column")) if f]
        filters = [{"column": normalize_ref(str(c)), "op": str(o), "value": str(v)}
                   for c, o, v in zip(form.getlist("filter_column"), form.getlist("filter_op"), form.getlist("filter_value"))
                   if c]  # the page always shows a blank row; skip it
        return {
            "columns": [normalize_ref(str(c)) for c in form.getlist("columns")],
            # An output name ("count") or a column reference; the builder resolves either.
            "filters": filters, "joins": joins, "aggregates": aggregates, "order_by": str(form.get("order_by", "")),
            "descending": bool(form.get("descending")), "limit": str(form.get("limit", "")).strip(),
        }

    def to_request(ctx: dict, state: dict) -> QueryRequest:
        schema, table = ctx["table_key"]
        joins = []
        for j in state["joins"]:
            key = ctx["names"].get(j["table"])
            if key is None:
                raise QueryError(f"unknown table {j['table']}")
            if not j["left"] or not j["right"]:
                raise QueryError(f"choose both join columns for {j['table']}")
            joins.append(Join(j["kind"], key[0], key[1], j["left"], j["right"]))
        limit = state["limit"]
        if limit and not limit.isdecimal():
            raise QueryError("Limit must be a whole number.")
        return QueryRequest(
            schema, table, tuple(state["columns"]),
            tuple(Filter(f["column"], f["op"], f["value"]) for f in state["filters"]),
            state["order_by"] or None, state["descending"], int(limit) if limit else None,
            tuple(joins), tuple(Aggregate(a["func"], a["column"]) for a in state["aggregates"]),
        )

    def default_state() -> dict:
        return {"columns": [], "filters": [], "joins": [], "aggregates": [], "order_by": "",
                "descending": False, "limit": str(limits.default_rows)}

    def page(request: Request, user: str, status: int = 200, **ctx):
        state = ctx.pop("state", None) or default_state()
        tables = query_tables(ctx, state["joins"]) if ctx.get("table_key") else []
        has_joins = len(tables) > 1
        valid_refs = {f"{t['alias']}.{c}" for t in tables for c in t["columns"]}

        def ref_label(ref: str) -> str:
            alias, _, column = ref.partition(".")
            index = int(alias[1:]) if alias[1:].isdigit() else 0
            if index >= len(tables):
                return ref
            return f"{tables[index]['label']}.{column}" if has_joins else column

        context = {
            "user": user,
            "instances": list(config.instances.values()),
            "saved": list(config.saved_queries.values()),
            "operators": list(OPERATORS), "join_kinds": list(JOIN_KINDS), "aggregates_list": list(AGGREGATES),
            "max_rows": limits.max_rows, "max_joins": MAX_JOINS,
            "sel": {"instance": "", "database": "", "table": ""}, "tables": None, "browse_error": None,
            "tab": "rows", "info": None, "info_error": None,
            "state": state, "query_tables": tables, "ref_label": ref_label,
            "selected": {c for c in state["columns"] if c in valid_refs},
            "all_selected": not state["columns"] and not state["aggregates"],
            "agg_labels": [aggregate_label(a["func"], ref_label(a["column"]) if a["column"] else "")
                           for a in state["aggregates"] if " ".join(a["func"].upper().split()) in AGGREGATES],
            "result": None, "saved_values": {}, "environment": "",
        }
        context.update(ctx)
        inst = config.instances.get(context["sel"]["instance"])
        context["environment"] = inst.environment if inst else ""
        for key in ("table_key", "names", "catalog"):
            context.pop(key, None)
        result = context["result"]
        if result and "rows" in result:
            context["result_csv"] = to_csv(result["columns"], result["rows"])
        return TEMPLATES.TemplateResponse(request, "index.html", context, status_code=status)

    def suggestions_for(ctx: dict) -> list[dict]:
        if not ctx.get("table_key"):
            return []
        instance, database = ctx["sel"]["instance"], ctx["sel"]["database"]
        try:
            return dbs.table_info(instance, database, *ctx["table_key"])["join_suggestions"]
        except (QueryError, UnknownDatabase, DatabaseError, QueryTimeout):
            return []

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        username, password = str(form.get("username", "")), str(form.get("password", ""))
        if await run_in_threadpool(check_login, config.users, username, password):
            request.session.clear()
            request.session["user"] = username
            audit.write("login", user=username)
            return RedirectResponse("/", status_code=303)
        audit.write("login_failed", user=username[:64])
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": "Wrong username or password."}, status_code=401)

    @app.post("/logout")
    def logout(request: Request):
        user = request.session.get("user")
        request.session.clear()
        if user:
            audit.write("logout", user=user)
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, db: str = "", table: str = "", tab: str = "rows", refresh: bool = False):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        ctx = browse(db, table, refresh)
        ctx["tab"] = tab if tab in TABS else "rows"
        status = 200
        if ctx["table_key"] and ctx["tab"] != "rows":
            status, info = run_table_info(user, ctx["sel"]["instance"], ctx["sel"]["database"], table)
            ctx["info" if status == 200 else "info_error"] = info if status == 200 else info["error"]
        elif ctx["table_key"]:
            ctx["suggestions"] = suggestions_for(ctx)
        return page(request, user, status=status, **ctx)

    @app.post("/run", response_class=HTMLResponse)
    async def run_form(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        db_value, table_value = str(form.get("db", "")), str(form.get("table", ""))
        ctx = await run_in_threadpool(browse, db_value, table_value)
        state = read_state(form)
        suggestions = await run_in_threadpool(suggestions_for, ctx)
        action = str(form.get("action", "run"))
        status, result = 200, None

        # Form-editing actions redraw the page without running anything.
        if (action.startswith("suggest:") and action[8:].isdecimal() and int(action[8:]) < len(suggestions)
                and len(state["joins"]) < MAX_JOINS):
            s = suggestions[int(action[8:])]
            state["joins"].append({"kind": s["kind"], "table": f"{s['schema']}.{s['table']}",
                                   "left": f"t0.{s['left']}", "right": s["right"]})
            state["columns"] = []
            action = "refresh"
        elif action == "add_join" and len(state["joins"]) < MAX_JOINS:
            state["joins"].append(dict(BLANK_JOIN, table=next(iter(ctx["names"]), "")))
            action = "refresh"
        elif action == "add_agg":
            if not state["aggregates"]:
                # The first aggregate turns selected columns into the GROUP BY; a page with
                # every column ticked by default would otherwise group by all of them.
                state["columns"] = []
            state["aggregates"].append({"func": "COUNT(*)", "column": ""})
            action = "refresh"
        elif action.startswith(("remove_join:", "remove_agg:")):
            kind, _, index = action.partition(":")
            items = state["joins" if kind == "remove_join" else "aggregates"]
            if index.isdecimal() and int(index) < len(items):
                items.pop(int(index))
                if kind == "remove_join":
                    drop_alias(state, int(index) + 1)
            action = "refresh"

        if ctx["table_key"] is None:
            status, result = 400, {"error": ctx.get("browse_error") or "Choose a table first."}
        elif action in ("run", "explain"):
            instance, _, database = db_value.partition("/")
            try:
                req = to_request(ctx, state)
            except QueryError as e:
                status, result = 400, {"error": str(e)}
            else:
                runner = run_explain if action == "explain" else run_table
                status, result = await run_in_threadpool(runner, user, instance, database, req)
        return page(request, user, status=status, state=state, result=result, suggestions=suggestions, **ctx)

    @app.post("/saved/run", response_class=HTMLResponse)
    async def saved_form(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        values = {k.removeprefix("param_"): str(v) for k, v in form.items() if k.startswith("param_")}
        status, result = await run_in_threadpool(run_saved, user, str(form.get("key", "")), values)
        return page(request, user, status=status, result=result, saved_values=values, saved_key=str(form.get("key", "")))

    # --- JSON API ---

    def signed_out():
        return JSONResponse({"error": "not signed in"}, status_code=401)

    @app.get("/api/databases")
    def api_databases(request: Request):
        if current_user(request) is None:
            return signed_out()
        return {"databases": [
            {"instance": inst.key, "title": inst.title, "database": database}
            for inst in config.instances.values() for database in inst.databases
        ]}

    @app.get("/api/tables")
    def api_tables(request: Request, db: str = "", refresh: bool = False):
        if current_user(request) is None:
            return signed_out()
        instance, _, database = db.partition("/")
        try:
            catalog = dbs.catalog(instance, database, refresh=refresh)
        except (UnknownDatabase, DatabaseError, QueryTimeout) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"tables": [
            {"schema": schema, "table": table, "name": f"{schema}.{table}", "columns": columns}
            for (schema, table), columns in sorted(catalog.items())
        ]}

    @app.get("/api/table-info")
    def api_table_info(request: Request, db: str = "", table: str = ""):
        user = current_user(request)
        if user is None:
            return signed_out()
        instance, _, database = db.partition("/")
        status, payload = run_table_info(user, instance, database, table)
        return JSONResponse(payload, status_code=status)

    @app.get("/api/saved")
    def api_saved_list(request: Request):
        if current_user(request) is None:
            return signed_out()
        # The SQL stays server-side; callers only need what to pass.
        return {"saved": [
            {"key": q.key, "title": q.title, "instance": q.instance, "database": q.database, "params": list(q.params)}
            for q in config.saved_queries.values()
        ]}

    @app.post("/api/query")
    def api_query(request: Request, body: TableQueryIn):
        user = current_user(request)
        if user is None:
            return signed_out()
        status, payload = run_table(user, body.instance, body.database, _request_from_api(body))
        return JSONResponse(payload, status_code=status)

    @app.post("/api/explain")
    def api_explain(request: Request, body: TableQueryIn):
        user = current_user(request)
        if user is None:
            return signed_out()
        status, payload = run_explain(user, body.instance, body.database, _request_from_api(body))
        return JSONResponse(payload, status_code=status)

    @app.post("/api/saved/{key}")
    def api_saved(request: Request, key: str, values: dict[str, str] | None = Body(default=None)):
        user = current_user(request)
        if user is None:
            return signed_out()
        status, payload = run_saved(user, key, values or {})
        return JSONResponse(payload, status_code=status)

    return app

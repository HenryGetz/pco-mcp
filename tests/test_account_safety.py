"""Account-safety and write-isolation invariants for the PCO MCP.

Covers seven areas:
1. AST guard: server.py and pco_accounts.py never call an HTTP write verb;
   pco_writes.py is the sole module with a mutating transport call.
2. Tool-surface guard: every pco_* tool is on the reviewed allowlist, and the
   nine write tools expose `account` (no default) plus `confirm=False`.
3. Alternating account routing under repeated calls cannot leak rows.
4. Chat selection (pco_use_account) versus explicit account= override.
5. Concurrent explicit-account reads stay isolated.
6. account="all" aggregation tags each plan with its own account/org.
7. Write tools never accept the chat selection or "all" as a target.

No network access: all clients are the fake clients from test_server_tools.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pco_accounts as pa
import server
from tests.test_server_tools import (
    CREDS,
    DualAccountMixin,
    FakeClient,
    FakeResponse,
    ServerTestBase,
    _path_only,
    ok,
)


REPO_ROOT = Path(__file__).resolve().parent.parent

# HTTP verbs that mutate state (or that could tunnel one). server.py and
# pco_accounts.py may only GET; every mutation goes through pco_writes.
FORBIDDEN_METHOD_NAMES = frozenset({"post", "put", "patch", "delete", "request", "send"})
FORBIDDEN_QUALIFIED_CALLS = frozenset(
    {"httpx.request", "httpx.Client.request", "httpx.Client.send"}
)

# The nine audited write tools. Each requires an explicit account (no default,
# never "all"/the chat selection) and previews unless confirm=True is passed.
WRITE_TOOL_NAMES = frozenset(
    {
        "pco_create_plan",
        "pco_update_plan",
        "pco_delete_plan",
        "pco_create_plan_item",
        "pco_update_plan_item",
        "pco_delete_plan_item",
        "pco_create_plan_time",
        "pco_update_plan_time",
        "pco_delete_plan_time",
    }
)

# Every pco_* tool must be reviewed before it may appear. Reads are marked
# below; the pco_*_plan/item/time entries are the audited writes whose only
# transport is pco_writes.run_write, and pco_use_account /
# pco_set_default_account only touch local chat/config state.
ALLOWED_TOOL_NAMES = frozenset(
    {
        "pco_list_accounts",
        "pco_account_status",
        "pco_use_account",
        "pco_set_default_account",
        "pco_list_service_types",
        "pco_list_plans",
        "pco_get_plan",
        "pco_search_songs",
        "pco_get_song",
        "pco_person_song_history",
        "pco_person_upcoming",
    }
) | WRITE_TOOL_NAMES

# Expected surface; a silently empty list_tools() must not pass.
REQUIRED_TOOL_NAMES = frozenset(
    {"pco_list_service_types", "pco_list_plans", "pco_use_account"}
)


def _tool_parameters(tool: object) -> dict[str, inspect.Parameter]:
    """Signature parameters of a tool, unwrapping a FastMCP FunctionTool if needed."""
    target = tool
    for attr in ("fn", "func", "__wrapped__"):
        candidate = getattr(target, attr, None)
        if callable(candidate):
            target = candidate
            break
    return inspect.signature(target).parameters


def _dotted_name(node: ast.AST) -> str | None:
    """Resolve ``httpx.Client.request``-style attribute chains to a dotted name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _find_forbidden_http_calls(source: str, filename: str) -> list[str]:
    """Parse ``source`` and return one message per forbidden write-style call."""
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    seen: set[tuple[int, int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        qualified = _dotted_name(func)
        if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_METHOD_NAMES:
            pass
        elif qualified in FORBIDDEN_QUALIFIED_CALLS:
            pass
        else:
            continue
        key = (node.lineno, node.col_offset, qualified or "")
        if key in seen:
            continue
        seen.add(key)
        violations.append(
            f"{filename}:{node.lineno}:{node.col_offset} issues forbidden HTTP "
            f"write call {qualified!r}"
        )
    return violations


class WriteTransportGuardTests(unittest.TestCase):
    """The security invariant: pco_writes is the only mutating transport."""

    def test_no_http_write_calls_in_server_or_accounts(self):
        violations: list[str] = []
        for name in ("server.py", "pco_accounts.py"):
            path = REPO_ROOT / name
            self.assertTrue(path.is_file(), f"missing source file {path}")
            violations.extend(
                _find_forbidden_http_calls(
                    path.read_text(encoding="utf-8"), name
                )
            )

        if violations:
            self.fail(
                "Write isolation was broken; server.py and pco_accounts.py must "
                "only GET through server._get, and pco_writes.run_write must be "
                "the sole mutating path:\n" + "\n".join(violations)
            )

    def test_pco_writes_is_the_sole_mutating_transport(self):
        write_path = REPO_ROOT / "pco_writes.py"
        self.assertTrue(write_path.is_file(), f"missing source file {write_path}")

        per_file: dict[str, list[str]] = {}
        for path in sorted(REPO_ROOT.glob("*.py")):
            violations = _find_forbidden_http_calls(
                path.read_text(encoding="utf-8"), path.name
            )
            if violations:
                per_file[path.name] = violations

        self.assertEqual(
            sorted(per_file),
            ["pco_writes.py"],
            f"only pco_writes.py may call a mutating HTTP verb: {per_file}",
        )
        write_calls = per_file["pco_writes.py"]
        # A guard that finds nothing here would be vacuous; exactly one
        # client.request call site is the single audited send path.
        self.assertEqual(len(write_calls), 1, write_calls)
        self.assertIn(".request", write_calls[0])
        self.assertFalse(
            any(
                token in write_calls[0]
                for token in (".post", ".put", ".patch", ".delete", ".send")
            ),
            write_calls,
        )

        source = write_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename="pco_writes.py")
        request_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "request"
        ]
        self.assertEqual(len(request_calls), 1, request_calls)
        target = request_calls[0]
        self.assertIsInstance(target.func.value, ast.Name)
        self.assertEqual(target.func.value.id, "client")
        self.assertTrue(target.args, target)
        self.assertIsInstance(target.args[0], ast.Name)
        self.assertEqual(target.args[0].id, "method")
        owners = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(call is target for call in ast.walk(node))
        ]
        self.assertEqual(owners, ["_send_write"], owners)

        ambient_names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        } | {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        }
        self.assertEqual(
            ambient_names & {"selected_account", "installation_default", "_dispatch", "fan_out"},
            set(),
            "pco_writes must not consult read-routing or ambient account state",
        )

    def test_guard_detects_synthetic_write_calls(self):
        source = """
import httpx

client.post("/mutate")
other.send("payload")
httpx.request("POST", "/mutate")
httpx.Client(base_url="https://example.test")
client.get("/read")
response.json()
"""
        violations = _find_forbidden_http_calls(source, "synthetic.py")

        self.assertEqual(len(violations), 3, violations)
        self.assertTrue(any("synthetic.py:4" in line for line in violations), violations)
        self.assertTrue(any("synthetic.py:5" in line for line in violations), violations)
        self.assertTrue(any("synthetic.py:6" in line for line in violations), violations)
        self.assertFalse(any("client.get" in line for line in violations), violations)


class ToolSurfaceAllowlistTests(unittest.TestCase):
    def test_every_pco_tool_is_on_the_reviewed_allowlist(self):
        tools = asyncio.run(server.mcp.list_tools())
        names = {tool.name for tool in tools if tool.name.startswith("pco_")}

        self.assertTrue(names, "list_tools() returned no pco_* tools")
        self.assertEqual(
            sorted((REQUIRED_TOOL_NAMES | WRITE_TOOL_NAMES) - names),
            [],
            f"core read or write tools disappeared from the surface: {sorted(names)}",
        )
        unexpected = sorted(names - ALLOWED_TOOL_NAMES)
        self.assertEqual(
            unexpected,
            [],
            "New pco_* tool(s) need a review before joining the "
            f"allowlist: {unexpected}",
        )

    def test_write_tools_require_account_without_a_default(self):
        for name in sorted(WRITE_TOOL_NAMES):
            with self.subTest(tool=name):
                tool = getattr(server, name, None)
                self.assertIsNotNone(tool, f"missing write tool {name}")
                params = _tool_parameters(tool)
                self.assertIn("account", params, f"{name} must take account")
                self.assertIs(
                    params["account"].default,
                    inspect.Parameter.empty,
                    f"{name} must require an explicit account (no default)",
                )

    def test_write_tools_default_to_preview(self):
        for name in sorted(WRITE_TOOL_NAMES):
            with self.subTest(tool=name):
                params = _tool_parameters(getattr(server, name))
                self.assertIn("confirm", params, f"{name} must take confirm")
                self.assertIs(
                    params["confirm"].default,
                    False,
                    f"{name} must default confirm=False (preview)",
                )


STRESS_ORGS = {
    "unw": {
        "id": "st-unw-stress",
        "name": "Main Sunday Morning",
        "org": "Main Campus",
    },
    "rs": {
        "id": "st-rs-stress",
        "name": "RS Saturday Evening",
        "org": "North Campus",
    },
}


def single_service_type_handler(account: str):
    """Serve exactly one service type, unique to this organization."""
    spec = STRESS_ORGS[account]
    payload = {
        "data": [
            {
                "id": spec["id"],
                "type": "ServiceType",
                "attributes": {"name": spec["name"], "frequency": "Weekly"},
            }
        ]
    }

    def handler(path, params):
        if _path_only(path) == "/service_types":
            return ok(payload)
        return FakeResponse(
            404, {"errors": [{"title": f"{account} has no route {_path_only(path)}"}]}
        )

    return handler


class AccountSwitchingStressTests(ServerTestBase):
    def test_alternating_accounts_never_route_or_leak_across_orgs(self):
        self.write_creds(CREDS)
        clients = {
            account: FakeClient(single_service_type_handler(account))
            for account in ("unw", "rs")
        }
        self.use_clients(clients)

        for iteration in range(60):
            requested = "unw" if iteration % 2 == 0 else "rs"
            other = "rs" if requested == "unw" else "unw"
            expected = STRESS_ORGS[requested]
            other_calls_before = len(clients[other].calls)

            self.client_calls.clear()
            rows = server.pco_list_service_types(account=requested)

            self.assertEqual(
                self.client_calls, [requested], f"iteration {iteration}: wrong client"
            )
            self.assertEqual(
                len(clients[other].calls),
                other_calls_before,
                f"iteration {iteration}: {other} client was called",
            )
            self.assertEqual(len(rows), 1, f"iteration {iteration}: {rows}")
            row = rows[0]
            self.assertEqual(row["account"], requested)
            self.assertEqual(row["org_name"], expected["org"])
            self.assertEqual(row["account_source"], "explicit")
            self.assertEqual(row["id"], expected["id"])
            self.assertEqual(row["name"], expected["name"])

        self.assertEqual(len(clients["unw"].calls), 30)
        self.assertEqual(len(clients["rs"].calls), 30)


class ChatSelectionOverrideTests(DualAccountMixin, ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)
        self.use_two_accounts()

    def test_selection_routes_omitted_explicit_wins_and_clear_restores_default(self):
        used = server.pco_use_account("rs")
        self.assertEqual(used["selected_account"], "rs")
        self.assertEqual(used["effective_source"], "chat")

        # 1. Omitted -> the chat selection.
        self.client_calls.clear()
        rows = server.pco_list_service_types()
        self.assertEqual(self.client_calls, ["rs"])
        self.assertEqual(
            {(row["account"], row["org_name"], row["account_source"]) for row in rows},
            {("rs", "North Campus", "chat")},
        )
        self.assertEqual(self.unw_client.calls, [])

        # 2. Explicit account= overrides the selection.
        rs_calls_before = len(self.rs_client.calls)
        self.client_calls.clear()
        rows = server.pco_list_service_types(account="unw")
        self.assertEqual(self.client_calls, ["unw"])
        self.assertEqual(
            {(row["account"], row["org_name"], row["account_source"]) for row in rows},
            {("unw", "Main Campus", "explicit")},
        )
        self.assertEqual(len(self.rs_client.calls), rs_calls_before)
        self.assertEqual(pa.selected_account(), "rs")

        # 3. Omitted again -> still the chat selection.
        self.client_calls.clear()
        rows = server.pco_list_service_types()
        self.assertEqual(self.client_calls, ["rs"])
        self.assertEqual(rows[0]["account"], "rs")
        self.assertEqual(rows[0]["account_source"], "chat")

        # 4. Clearing the selection restores the installation default (first creds key).
        cleared = server.pco_use_account()
        self.assertIsNone(cleared["selected_account"])
        self.assertIsNone(pa.selected_account())
        self.assertEqual(cleared["effective_account"], "unw")
        self.assertEqual(cleared["effective_source"], "installation")

        self.client_calls.clear()
        rows = server.pco_list_service_types()
        self.assertEqual(self.client_calls, ["unw"])
        self.assertEqual(
            {(row["account"], row["org_name"], row["account_source"]) for row in rows},
            {("unw", "Main Campus", "installation")},
        )
        self.assertFalse(self.config_path.exists())


class ConcurrentExplicitReadTests(DualAccountMixin, ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)
        self.use_two_accounts()
        server.pco_use_account("rs")

    def test_concurrent_explicit_reads_stay_on_their_own_account(self):
        orgs = {
            "unw": ("Main Campus", "st-unw"),
            "rs": ("North Campus", "st-rs"),
        }
        tasks = ["unw"] * 20 + ["rs"] * 20

        def read(account: str):
            rows = server.pco_list_service_types(account=account)
            org, service_type_id = orgs[account]
            if not rows:
                raise AssertionError(f"{account}: empty result")
            for row in rows:
                if row["account"] != account:
                    raise AssertionError(f"{account}: leaked row {row!r}")
                if row["org_name"] != org:
                    raise AssertionError(f"{account}: wrong org {row!r}")
                if row["id"] != service_type_id:
                    raise AssertionError(f"{account}: wrong service type {row!r}")
            return account

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(read, account) for account in tasks]
            results = [future.result(timeout=60) for future in futures]

        self.assertEqual(results, tasks)
        self.assertEqual(pa.selected_account(), "rs")
        self.assertEqual(len(self.unw_client.calls), 20)
        self.assertEqual(len(self.rs_client.calls), 20)


def upcoming_handler(account: str, service_type_id: str, service_type_name: str,
                     person_id: str, days_ahead: int):
    """Serve exactly one matching future plan for one organization."""
    plan_date = date.today() + timedelta(days=days_ahead)
    sort_date = plan_date.isoformat()
    service_type = {
        "id": service_type_id,
        "type": "ServiceType",
        "attributes": {"name": service_type_name, "frequency": "Weekly"},
    }
    plan = {
        "id": f"plan-{account}",
        "type": "Plan",
        "attributes": {
            "title": f"{service_type_name} Plan",
            "dates": plan_date.strftime("%B %d, %Y"),
            "sort_date": sort_date,
        },
    }
    team_member = {
        "id": f"tm-{account}",
        "type": "TeamMember",
        "attributes": {
            "status": "C",
            "team_position_name": "Acoustic Guitar",
            "name": "Alex Morgan",
        },
        "relationships": {
            "person": {"data": {"id": person_id, "type": "Person"}}
        },
    }
    plan_time = {
        "id": f"time-{account}",
        "type": "PlanTime",
        "attributes": {
            "name": "Service",
            "time_type": "service",
            "starts_at": f"{sort_date}T10:30:00Z",
            "ends_at": f"{sort_date}T12:00:00Z",
        },
    }
    person = {
        "id": person_id,
        "type": "Person",
        "attributes": {"full_name": "Alex Morgan", "archived": False},
    }

    def handler(path, params):
        p = _path_only(path)
        if p == "/people":
            return ok({"data": [person]})
        if p == "/service_types":
            return ok({"data": [service_type]})
        if p == f"/service_types/{service_type_id}":
            return ok({"data": service_type})
        if p == f"/service_types/{service_type_id}/plans":
            return ok({"data": [plan]})
        if p == f"/service_types/{service_type_id}/plans/plan-{account}/team_members":
            return ok({"data": [team_member]})
        if p == f"/service_types/{service_type_id}/plans/plan-{account}/plan_times":
            return ok({"data": [plan_time]})
        return FakeResponse(
            404, {"errors": [{"title": f"{account} has no route {p}"}]}
        )

    return handler


class UpcomingAggregationSafetyTests(ServerTestBase):
    def test_upcoming_all_tags_each_plan_with_its_own_account_and_org(self):
        upcoming_tool = getattr(server, "pco_person_upcoming", None)
        if upcoming_tool is None:
            self.skipTest("pco_person_upcoming is not implemented yet")

        self.write_creds(CREDS)
        specs = {
            "unw": {
                "service_type_id": "st-unw",
                "service_type_name": "Main Sunday Morning",
                "person_id": "person-unw",
                "org": "Main Campus",
                "days_ahead": 3,
            },
            "rs": {
                "service_type_id": "st-rs",
                "service_type_name": "RS Saturday Evening",
                "person_id": "person-rs",
                "org": "North Campus",
                "days_ahead": 4,
            },
        }
        self.use_clients(
            {
                account: FakeClient(
                    upcoming_handler(
                        account,
                        spec["service_type_id"],
                        spec["service_type_name"],
                        spec["person_id"],
                        spec["days_ahead"],
                    )
                )
                for account, spec in specs.items()
            }
        )

        envelope = upcoming_tool(person_name="Alex Morgan", account="all")

        self.assertEqual(envelope["account"], "all")
        self.assertEqual(envelope["account_source"], "all")
        self.assertEqual(envelope["errors"], [])
        self.assertEqual(envelope["summary"]["accounts_succeeded"], 2)
        self.assertEqual(envelope["summary"]["matched_plans"], 2)

        plans = envelope["plans"]
        self.assertEqual(len(plans), 2)
        self.assertEqual({plan["account"] for plan in plans}, {"unw", "rs"})

        for plan in plans:
            account = plan["account"]
            spec = specs[account]
            self.assertEqual(plan["org_name"], spec["org"])
            self.assertEqual(plan["service_type_id"], spec["service_type_id"])
            self.assertEqual(plan["service_type_name"], spec["service_type_name"])
            self.assertEqual(plan["assignments"][0]["person_id"], spec["person_id"])
            self.assertEqual(plan["assignments"][0]["name"], "Alex Morgan")

        # The per-account envelopes must agree with the flattened list.
        by_account = {row["account"]: row for row in envelope["results"]}
        self.assertEqual(set(by_account), {"unw", "rs"})
        for account, row in by_account.items():
            spec = specs[account]
            self.assertEqual(row["org_name"], spec["org"])
            self.assertEqual(len(row["data"]["plans"]), 1)
            inner = row["data"]["plans"][0]
            self.assertEqual(inner["account"], account)
            self.assertEqual(inner["org_name"], spec["org"])
            self.assertEqual(inner["service_type_id"], spec["service_type_id"])

        # No cross-account leak in either direction.
        self.assertEqual(
            {(plan["account"], plan["plan_id"]) for plan in plans},
            {("unw", "plan-unw"), ("rs", "plan-rs")},
        )


if __name__ == "__main__":
    unittest.main()

"""PCO write-surface tests: previews, explicit-account isolation, payloads, retries.

No network access and no credentials. ``pco_accounts.get_client`` (and every
module-level alias of it, including pco_writes.get_client) is replaced with
fake clients that serve canned preflight payloads and record mutating
``.request`` calls. A fake client with no writer raises on any request, so a
test that accidentally writes fails loudly instead of passing.
"""

from __future__ import annotations

import unittest

import httpx

import pco_accounts as pa
import pco_writes
import server
from tests.test_server_tools import (
    CREDS,
    FakeClient,
    FakeResponse,
    ServerTestBase,
    _path_only,
    ok,
)


START = "2026-11-22T10:30:00Z"
END = "2026-11-22T12:00:00Z"

ACCOUNT_ERRORS = (pco_writes.WriteError, pa.AccountError, ValueError)


class NoContentResponse(FakeResponse):
    """A real 204 has an empty body; parsing it as JSON must fail."""

    def __init__(self, status_code=204, headers=None):
        super().__init__(status_code, payload={}, headers=headers, text="")

    def json(self):
        raise ValueError("204 No Content has no JSON body")


def created_response(resource_type: str = "Plan", resource_id: str = "901", status: int = 201):
    """Respond to a successful POST/PATCH with one JSON:API resource."""

    def responder(call):
        return FakeResponse(
            status,
            {"data": {"id": resource_id, "type": resource_type, "attributes": {}}},
            headers={"x-request-id": "req-1"},
        )

    return responder


def write_org_handler(
    account: str,
    *,
    service_type_id: str = "101",
    plan_id: str = "201",
    item_id: str = "301",
    plan_time_id: str = "401",
    song_id: str = "501",
    plan_title: str | None = "Existing Plan",
    plan_sort_date: str = "2026-11-22",
    item_title: str = "Existing Item",
    plan_status: int = 200,
):
    """Canned preflight GETs for one organization; 404 for anything else."""
    service_type = {
        "id": service_type_id,
        "type": "ServiceType",
        "attributes": {"name": "Sunday Morning", "frequency": "Weekly"},
    }
    plan = {
        "id": plan_id,
        "type": "Plan",
        "attributes": {
            "title": plan_title,
            "sort_date": plan_sort_date,
            "dates": "November 22, 2026",
            "public": False,
        },
    }
    item = {
        "id": item_id,
        "type": "Item",
        "attributes": {
            "title": item_title,
            "item_type": "item",
            "sequence": 2,
            "length": 300,
            "description": None,
            "service_position": "during",
        },
    }
    plan_time = {
        "id": plan_time_id,
        "type": "PlanTime",
        "attributes": {
            "name": "Service",
            "time_type": "service",
            "starts_at": START,
            "ends_at": END,
        },
    }
    song = {
        "id": song_id,
        "type": "Song",
        "attributes": {"title": "Great Are You Lord"},
    }
    plan_path = f"/service_types/{service_type_id}/plans/{plan_id}"

    def handler(path, params):
        p = _path_only(path)
        if p == "/service_types":
            return ok({"data": [service_type]})
        if p == f"/service_types/{service_type_id}":
            return ok({"data": service_type})
        if p == f"/service_types/{service_type_id}/plans":
            return ok({"data": [plan]})
        if p == plan_path:
            if plan_status != 200:
                return FakeResponse(
                    plan_status,
                    {"errors": [{"title": f"no plan {plan_id} for {account}"}]},
                )
            return ok({"data": plan})
        if p == f"{plan_path}/items":
            return ok({"data": [item]})
        if p == f"{plan_path}/items/{item_id}":
            return ok({"data": item})
        if p == f"{plan_path}/plan_times":
            return ok({"data": [plan_time]})
        if p == f"/service_types/{service_type_id}/plan_times/{plan_time_id}":
            return ok({"data": plan_time})
        if p == f"/songs/{song_id}":
            return ok({"data": song})
        return FakeResponse(
            404, {"errors": [{"title": f"{account} has no route {p}"}]}
        )

    return handler


class WriteTestBase(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)
        self.write_requests: list[dict] = []

    def use_write_clients(self, responder=None, **handler_kwargs):
        self.write_requests = []
        responder = responder or created_response()

        def make_writer(account):
            def writer(call):
                self.write_requests.append({"account": account, **call})
                return responder(call)

            return writer

        self.unw_client = FakeClient(
            write_org_handler("unw", **handler_kwargs), writer=make_writer("unw")
        )
        self.rs_client = FakeClient(
            write_org_handler("rs", **handler_kwargs), writer=make_writer("rs")
        )
        self.use_clients({"unw": self.unw_client, "rs": self.rs_client})
        return self.unw_client, self.rs_client

    def assert_no_writes(self):
        self.assertEqual(self.write_requests, [])
        self.assertEqual(self.unw_client.request_calls, [])
        self.assertEqual(self.rs_client.request_calls, [])

    def only_write(self) -> dict:
        self.assertEqual(len(self.write_requests), 1, self.write_requests)
        return self.write_requests[0]

    def tool_cases(self):
        """(name, callable, kwargs) for each of the nine write tools."""
        return [
            (
                "pco_create_plan",
                server.pco_create_plan,
                {"service_type_id": "101", "title": "Advent Plan"},
            ),
            (
                "pco_update_plan",
                server.pco_update_plan,
                {
                    "service_type_id": "101",
                    "plan_id": "201",
                    "title": "Renamed Plan",
                    "expected_title": "Existing Plan",
                },
            ),
            (
                "pco_delete_plan",
                server.pco_delete_plan,
                {
                    "service_type_id": "101",
                    "plan_id": "201",
                    "expected_title": "Existing Plan",
                },
            ),
            (
                "pco_create_plan_item",
                server.pco_create_plan_item,
                {"service_type_id": "101", "plan_id": "201", "title": "Welcome"},
            ),
            (
                "pco_update_plan_item",
                server.pco_update_plan_item,
                {
                    "service_type_id": "101",
                    "plan_id": "201",
                    "item_id": "301",
                    "title": "Renamed Item",
                    "expected_title": "Existing Item",
                },
            ),
            (
                "pco_delete_plan_item",
                server.pco_delete_plan_item,
                {
                    "service_type_id": "101",
                    "plan_id": "201",
                    "item_id": "301",
                    "expected_title": "Existing Item",
                },
            ),
            (
                "pco_create_plan_time",
                server.pco_create_plan_time,
                {
                    "service_type_id": "101",
                    "plan_id": "201",
                    "starts_at": START,
                    "ends_at": END,
                },
            ),
            (
                "pco_update_plan_time",
                server.pco_update_plan_time,
                {
                    "service_type_id": "101",
                    "plan_id": "201",
                    "plan_time_id": "401",
                    "name": "Rehearsal",
                    "expected_starts_at": START,
                },
            ),
            (
                "pco_delete_plan_time",
                server.pco_delete_plan_time,
                {
                    "service_type_id": "101",
                    "plan_id": "201",
                    "plan_time_id": "401",
                    "expected_starts_at": START,
                },
            ),
        ]


class WritePreviewTests(WriteTestBase):
    def test_every_write_tool_previews_without_a_mutating_request(self):
        self.use_write_clients()

        for name, tool, kwargs in self.tool_cases():
            with self.subTest(tool=name):
                result = tool(account="unw", **kwargs)

                self.assertEqual(result["mode"], "preview")
                self.assertIs(result["would_write"], False)
                self.assertIs(result["confirm_required"], True)
                self.assertEqual(result["account"], "unw")
                self.assertEqual(result["org_name"], "Main Campus")
                self.assertEqual(result["account_source"], "explicit")
                self.assertEqual(result["operation"], name.removeprefix("pco_"))
                self.assertIsInstance(result["preflight"], dict)
                self.assertIn("confirm=True", result["message"])
                if result["method"] == "DELETE":
                    self.assertIsNone(result["payload"])
                else:
                    self.assertIn("data", result["payload"])

        self.assert_no_writes()
        self.assertEqual(len(self.unw_client.calls) > 0, True)
        self.assertEqual(self.rs_client.calls, [])


class WriteAccountIsolationTests(WriteTestBase):
    def test_explicit_account_wins_over_chat_selection(self):
        self.use_write_clients(created_response("Plan", "901"))
        self.assertEqual(server.pco_use_account("rs")["selected_account"], "rs")

        preview = server.pco_create_plan(
            account="unw", service_type_id="101", title="Advent Plan"
        )
        self.assertEqual(preview["account"], "unw")
        self.assertEqual(preview["org_name"], "Main Campus")
        self.assertEqual(set(self.client_calls), {"unw"})

        result = server.pco_create_plan(
            account="unw", service_type_id="101", title="Advent Plan", confirm=True
        )

        call = self.only_write()
        self.assertEqual(call["account"], "unw")
        self.assertEqual(result["account"], "unw")
        self.assertEqual(result["account_source"], "explicit")
        self.assertEqual(self.rs_client.calls, [])
        self.assertEqual(self.rs_client.request_calls, [])
        self.assertEqual(set(self.client_calls), {"unw"})
        # The chat selection itself is untouched by a write.
        self.assertEqual(pa.selected_account(), "rs")

    def test_selected_account_never_supplies_a_write_target(self):
        self.use_write_clients()
        server.pco_use_account("rs")

        with self.assertRaises(ACCOUNT_ERRORS):
            server.pco_create_plan(
                account=None, service_type_id="101", title="Advent Plan", confirm=True
            )

        self.assert_no_writes()
        self.assertEqual(self.unw_client.calls, [])
        self.assertEqual(self.rs_client.calls, [])
        self.assertEqual(pa.selected_account(), "rs")

    def test_all_bad_accounts_are_rejected_before_any_write(self):
        self.use_write_clients()
        for name, tool, kwargs in self.tool_cases():
            for bad in (None, "", "   ", "all", "ALL", "bogus"):
                with self.subTest(tool=name, account=bad):
                    with self.assertRaises(ACCOUNT_ERRORS):
                        tool(account=bad, confirm=True, **kwargs)
        self.assert_no_writes()
        # Rejection happens before any preflight GET too.
        self.assertEqual(self.unw_client.calls, [])
        self.assertEqual(self.rs_client.calls, [])

    def test_missing_account_is_a_type_error(self):
        self.use_write_clients()
        for name, tool, kwargs in self.tool_cases():
            with self.subTest(tool=name):
                with self.assertRaises(TypeError):
                    tool(**kwargs)
        self.assert_no_writes()


class PlanWriteTests(WriteTestBase):
    def test_create_plan_preview_then_one_post(self):
        self.use_write_clients(created_response("Plan", "901", status=201))

        preview = server.pco_create_plan(
            account="unw",
            service_type_id="101",
            title="Advent Plan",
            series_title="Advent 2026",
        )
        self.assertEqual(preview["method"], "POST")
        self.assertEqual(preview["path"], "/service_types/101/plans")
        self.assertEqual(preview["payload"]["data"]["type"], "Plan")
        self.assertNotIn("id", preview["payload"]["data"])
        self.assertEqual(
            preview["payload"]["data"]["attributes"],
            {"title": "Advent Plan", "public": False, "series_title": "Advent 2026"},
        )
        self.assertEqual(preview["preflight"]["id"], "101")
        self.assert_no_writes()

        result = server.pco_create_plan(
            account="unw",
            service_type_id="101",
            title="Advent Plan",
            series_title="Advent 2026",
            confirm=True,
        )

        call = self.only_write()
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["path"], "/service_types/101/plans")
        self.assertEqual(
            call["json"],
            {
                "data": {
                    "type": "Plan",
                    "attributes": {
                        "title": "Advent Plan",
                        "public": False,
                        "series_title": "Advent 2026",
                    },
                }
            },
        )
        headers = {key.lower(): value for key, value in call["headers"].items()}
        self.assertEqual(headers["content-type"], "application/vnd.api+json")
        self.assertEqual(headers["accept"], "application/vnd.api+json")

        self.assertEqual(result["mode"], "write")
        self.assertIs(result["written"], True)
        self.assertEqual(result["status_code"], 201)
        self.assertEqual(result["request_id"], "req-1")
        self.assertEqual(result["resource"]["id"], "901")
        self.assertIs(result["deleted"], False)
        self.assertEqual(result["account"], "unw")
        self.assertEqual(result["account_source"], "explicit")

    def test_update_plan_patches_only_supplied_attributes(self):
        self.use_write_clients(created_response("Plan", "201", status=200))

        result = server.pco_update_plan(
            account="unw",
            service_type_id="101",
            plan_id="201",
            title="Renamed Plan",
            public=True,
            reminders_disabled=True,
            expected_title="Existing Plan",
            confirm=True,
        )

        call = self.only_write()
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(call["path"], "/service_types/101/plans/201")
        self.assertEqual(
            call["json"],
            {
                "data": {
                    "type": "Plan",
                    "id": "201",
                    "attributes": {
                        "title": "Renamed Plan",
                        "public": True,
                        "reminders_disabled": True,
                    },
                }
            },
        )
        self.assertEqual(result["resource"]["id"], "201")

    def test_update_plan_expected_title_mismatch_sends_nothing(self):
        self.use_write_clients()

        with self.assertRaises(pco_writes.WriteError):
            server.pco_update_plan(
                account="unw",
                service_type_id="101",
                plan_id="201",
                title="Renamed Plan",
                expected_title="Not The Current Title",
                confirm=True,
            )

        self.assert_no_writes()

    def test_update_plan_requires_an_attribute(self):
        self.use_write_clients()
        with self.assertRaises(ValueError):
            server.pco_update_plan(
                account="unw",
                service_type_id="101",
                plan_id="201",
                expected_title="Existing Plan",
                confirm=True,
            )
        self.assert_no_writes()

    def test_delete_plan_exact_title_returns_204(self):
        self.use_write_clients(lambda call: NoContentResponse())

        result = server.pco_delete_plan(
            account="unw",
            service_type_id="101",
            plan_id="201",
            expected_title="Existing Plan",
            confirm=True,
        )

        call = self.only_write()
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(call["path"], "/service_types/101/plans/201")
        self.assertIsNone(call.get("json"))
        self.assertEqual(result["status_code"], 204)
        self.assertIs(result["deleted"], True)
        self.assertIsNone(result["resource"])
        self.assertIsNone(result["request_id"])

    def test_delete_plan_sort_date_guard_works_with_null_title(self):
        self.use_write_clients(lambda call: NoContentResponse(), plan_title=None)

        result = server.pco_delete_plan(
            account="unw",
            service_type_id="101",
            plan_id="201",
            expected_sort_date="2026-11-22",
            confirm=True,
        )

        call = self.only_write()
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(result["status_code"], 204)

    def test_delete_plan_requires_a_guard(self):
        self.use_write_clients(lambda call: NoContentResponse())
        with self.assertRaises(ValueError):
            server.pco_delete_plan(
                account="unw",
                service_type_id="101",
                plan_id="201",
                confirm=True,
            )
        self.assert_no_writes()


class ItemWriteTests(WriteTestBase):
    def test_create_item_payload_shapes(self):
        self.use_write_clients(created_response("Item", "302", status=201))

        server.pco_create_plan_item(
            account="unw",
            service_type_id="101",
            plan_id="201",
            title="Welcome",
            description="Greeting",
            length=120,
            sequence=1,
            confirm=True,
        )
        server.pco_create_plan_item(
            account="unw",
            service_type_id="101",
            plan_id="201",
            title="Opening",
            item_type="header",
            confirm=True,
        )
        server.pco_create_plan_item(
            account="unw",
            service_type_id="101",
            plan_id="201",
            title="Great Are You Lord",
            item_type="song",
            song_id="501",
            arrangement_id="601",
            key_id="701",
            length=240,
            confirm=True,
        )
        server.pco_create_plan_item(
            account="unw",
            service_type_id="101",
            plan_id="201",
            title="Great Are You Lord",
            item_type="item",
            song_id="501",
            confirm=True,
        )

        self.assertEqual(len(self.write_requests), 4, self.write_requests)
        for call in self.write_requests:
            self.assertEqual(call["method"], "POST")
            self.assertEqual(
                call["path"], "/service_types/101/plans/201/items"
            )
            self.assertEqual(call["json"]["data"]["type"], "Item")
            self.assertNotIn("id", call["json"]["data"])

        plain, header, song, song_as_item = self.write_requests
        plain_attrs = plain["json"]["data"]["attributes"]
        self.assertEqual(plain_attrs["title"], "Welcome")
        self.assertEqual(plain_attrs["description"], "Greeting")
        self.assertEqual(plain_attrs["length"], 120)
        self.assertEqual(plain_attrs["sequence"], 1)
        self.assertEqual(plain_attrs["service_position"], "during")
        self.assertNotIn("item_type", plain_attrs)
        self.assertNotIn("song_id", plain_attrs)

        header_attrs = header["json"]["data"]["attributes"]
        self.assertEqual(header_attrs["item_type"], "header")
        self.assertNotIn("song_id", header_attrs)

        song_attrs = song["json"]["data"]["attributes"]
        self.assertNotIn("item_type", song_attrs)
        self.assertEqual(song_attrs["song_id"], "501")
        self.assertEqual(song_attrs["arrangement_id"], "601")
        self.assertEqual(song_attrs["key_id"], "701")
        self.assertEqual(song_attrs["length"], 240)

        # item_type="item" plus a song_id is treated as a song.
        song_as_item_attrs = song_as_item["json"]["data"]["attributes"]
        self.assertNotIn("item_type", song_as_item_attrs)
        self.assertEqual(song_as_item_attrs["song_id"], "501")

        # The song preflight ran on the write account, not the other org.
        song_paths = [path for path, _ in self.unw_client.calls]
        self.assertIn("/songs/501", song_paths)
        self.assertEqual(self.rs_client.calls, [])

    def test_create_item_rejects_invalid_shapes_before_writing(self):
        self.use_write_clients()
        cases = [
            {"title": "No Song", "item_type": "song"},
            {"title": "Bad Type", "item_type": "nonsense"},
            {"title": "Header Song", "item_type": "header", "song_id": "501"},
            {"title": "Orphan Arrangement", "arrangement_id": "601"},
            {"title": "Orphan Key", "key_id": "701"},
        ]
        for kwargs in cases:
            with self.subTest(case=kwargs):
                with self.assertRaises(ValueError):
                    server.pco_create_plan_item(
                        account="unw",
                        service_type_id="101",
                        plan_id="201",
                        confirm=True,
                        **kwargs,
                    )
        self.assert_no_writes()

    def test_update_item_patches_supplied_fields(self):
        self.use_write_clients(created_response("Item", "301", status=200))

        server.pco_update_plan_item(
            account="unw",
            service_type_id="101",
            plan_id="201",
            item_id="301",
            title="Renamed Item",
            sequence=3,
            expected_title="Existing Item",
            confirm=True,
        )

        call = self.only_write()
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(
            call["path"], "/service_types/101/plans/201/items/301"
        )
        self.assertEqual(
            call["json"],
            {
                "data": {
                    "type": "Item",
                    "id": "301",
                    "attributes": {"title": "Renamed Item", "sequence": 3},
                }
            },
        )

    def test_update_item_guards(self):
        self.use_write_clients()

        with self.assertRaises(pco_writes.WriteError):
            server.pco_update_plan_item(
                account="unw",
                service_type_id="101",
                plan_id="201",
                item_id="301",
                title="Renamed Item",
                expected_title="Wrong Title",
                confirm=True,
            )
        with self.assertRaises(ValueError):
            server.pco_update_plan_item(
                account="unw",
                service_type_id="101",
                plan_id="201",
                item_id="301",
                expected_title="Existing Item",
                confirm=True,
            )

        self.assert_no_writes()

    def test_delete_item_exact_title_returns_204(self):
        self.use_write_clients(lambda call: NoContentResponse())

        result = server.pco_delete_plan_item(
            account="unw",
            service_type_id="101",
            plan_id="201",
            item_id="301",
            expected_title="Existing Item",
            confirm=True,
        )

        call = self.only_write()
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(
            call["path"], "/service_types/101/plans/201/items/301"
        )
        self.assertIsNone(call.get("json"))
        self.assertIs(result["deleted"], True)
        self.assertEqual(result["status_code"], 204)

    def test_delete_item_title_mismatch_sends_nothing(self):
        self.use_write_clients(lambda call: NoContentResponse())

        with self.assertRaises(pco_writes.WriteError):
            server.pco_delete_plan_item(
                account="unw",
                service_type_id="101",
                plan_id="201",
                item_id="301",
                expected_title="Wrong Title",
                confirm=True,
            )

        self.assert_no_writes()


class PlanTimeWriteTests(WriteTestBase):
    def test_create_plan_time_payload(self):
        self.use_write_clients(created_response("PlanTime", "402", status=201))

        server.pco_create_plan_time(
            account="unw",
            service_type_id="101",
            plan_id="201",
            starts_at=START,
            ends_at=END,
            name="Service",
            team_reminders={"1201": 3},
            confirm=True,
        )

        call = self.only_write()
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["path"], "/service_types/101/plans/201/plan_times"
        )
        self.assertEqual(
            call["json"],
            {
                "data": {
                    "type": "PlanTime",
                    "attributes": {
                        "starts_at": START,
                        "ends_at": END,
                        "time_type": "service",
                        "name": "Service",
                        "team_reminders": {"1201": 3},
                    },
                }
            },
        )

    def test_create_plan_time_rejects_invalid_values_before_writing(self):
        self.use_write_clients()
        cases = [
            {"starts_at": "2026-11-22T10:30:00", "ends_at": END},
            {"starts_at": START, "ends_at": "2026-11-22T09:00:00Z"},
            {"starts_at": START, "ends_at": START},
            {"starts_at": START, "ends_at": END, "time_type": "banquet"},
            {"starts_at": START, "ends_at": END, "team_reminders": {"1201": 9}},
            {"starts_at": START, "ends_at": END, "team_reminders": {"abc": 3}},
        ]
        for kwargs in cases:
            with self.subTest(case=kwargs):
                with self.assertRaises((ValueError, pco_writes.WriteError)):
                    server.pco_create_plan_time(
                        account="unw",
                        service_type_id="101",
                        plan_id="201",
                        confirm=True,
                        **kwargs,
                    )
        self.assert_no_writes()

    def test_update_plan_time_patches_base_path(self):
        self.use_write_clients(created_response("PlanTime", "401", status=200))

        server.pco_update_plan_time(
            account="unw",
            service_type_id="101",
            plan_id="201",
            plan_time_id="401",
            name="Rehearsal",
            time_type="rehearsal",
            expected_starts_at=START,
            confirm=True,
        )

        call = self.only_write()
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(call["path"], "/service_types/101/plan_times/401")
        self.assertEqual(
            call["json"],
            {
                "data": {
                    "type": "PlanTime",
                    "id": "401",
                    "attributes": {"name": "Rehearsal", "time_type": "rehearsal"},
                }
            },
        )
        # The plan-time collection was read before the write.
        self.assertIn(
            ("/service_types/101/plans/201/plan_times", {"per_page": 100}),
            self.unw_client.calls,
        )

    def test_update_plan_time_guards(self):
        self.use_write_clients()

        with self.assertRaises(pco_writes.WriteError):
            server.pco_update_plan_time(
                account="unw",
                service_type_id="101",
                plan_id="201",
                plan_time_id="401",
                name="Rehearsal",
                expected_starts_at="2026-11-22T11:30:00Z",
                confirm=True,
            )
        with self.assertRaises(ValueError):
            server.pco_update_plan_time(
                account="unw",
                service_type_id="101",
                plan_id="201",
                plan_time_id="401",
                expected_starts_at=START,
                confirm=True,
            )

        self.assert_no_writes()

    def test_update_plan_time_rejects_a_cross_field_inversion(self):
        self.use_write_clients()

        with self.assertRaises(ValueError):
            server.pco_update_plan_time(
                account="unw",
                service_type_id="101",
                plan_id="201",
                plan_time_id="401",
                starts_at="2026-11-22T13:00:00Z",
                confirm=True,
            )

        self.assert_no_writes()

    def test_delete_plan_time_base_path_returns_204(self):
        self.use_write_clients(lambda call: NoContentResponse())

        result = server.pco_delete_plan_time(
            account="unw",
            service_type_id="101",
            plan_id="201",
            plan_time_id="401",
            expected_starts_at=START,
            confirm=True,
        )

        call = self.only_write()
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(call["path"], "/service_types/101/plan_times/401")
        self.assertIsNone(call.get("json"))
        self.assertEqual(result["status_code"], 204)
        self.assertIs(result["deleted"], True)

    def test_delete_plan_time_expected_start_mismatch_sends_nothing(self):
        self.use_write_clients(lambda call: NoContentResponse())

        with self.assertRaises(pco_writes.WriteError):
            server.pco_delete_plan_time(
                account="unw",
                service_type_id="101",
                plan_id="201",
                plan_time_id="401",
                expected_starts_at="2026-11-22T11:30:00Z",
                confirm=True,
            )

        self.assert_no_writes()


class PreflightFailureTests(WriteTestBase):
    def test_preflight_404_fails_closed_without_a_write(self):
        self.use_write_clients(lambda call: NoContentResponse(), plan_status=404)

        with self.assertRaises(pco_writes.WriteError) as ctx:
            server.pco_update_plan(
                account="unw",
                service_type_id="101",
                plan_id="201",
                title="Renamed Plan",
                expected_title="Existing Plan",
                confirm=True,
            )

        message = str(ctx.exception)
        self.assertIn("404", message)
        self.assertIn("unw", message)
        self.assert_no_writes()
        self.assertEqual(set(self.client_calls), {"unw"})


class RunWriteTransportTests(WriteTestBase):
    def delete_args(self, **overrides):
        args = {
            "account": "unw",
            "operation": "delete_plan",
            "method": "DELETE",
            "path": "/service_types/101/plans/201",
            "resource_type": "Plan",
            "resource_id": "201",
            "attributes": None,
            "preflight": {"resource": "plan", "id": "201"},
        }
        args.update(overrides)
        return args

    def test_preview_shape_sends_nothing(self):
        self.use_write_clients()

        result = pco_writes.run_write(
            account="unw",
            operation="create_plan",
            method="POST",
            path="/service_types/101/plans",
            resource_type="Plan",
            resource_id=None,
            attributes={"title": "Advent Plan"},
            preflight={"resource": "service_type", "id": "101"},
        )

        self.assertEqual(result["mode"], "preview")
        self.assertIs(result["would_write"], False)
        self.assertIs(result["confirm_required"], True)
        self.assertEqual(result["account"], "unw")
        self.assertEqual(result["org_name"], "Main Campus")
        self.assertEqual(result["account_source"], "explicit")
        self.assertEqual(result["operation"], "create_plan")
        self.assertEqual(result["method"], "POST")
        self.assertEqual(result["path"], "/service_types/101/plans")
        self.assertEqual(
            result["payload"],
            {
                "data": {
                    "type": "Plan",
                    "attributes": {"title": "Advent Plan"},
                }
            },
        )
        self.assertEqual(result["preflight"]["id"], "101")
        self.assertIn("confirm=True", result["message"])
        self.assert_no_writes()
        self.assertEqual(self.unw_client.calls, [])
        self.assertEqual(self.unw_client.request_calls, [])

    def test_post_with_a_client_supplied_id_is_rejected(self):
        self.use_write_clients()
        with self.assertRaises(pco_writes.WriteError):
            pco_writes.run_write(
                account="unw",
                operation="create_plan",
                method="POST",
                path="/service_types/101/plans",
                resource_type="Plan",
                resource_id="901",
                attributes={"title": "Advent Plan"},
                preflight=None,
                confirm=True,
            )
        self.assert_no_writes()

    def test_429_is_retryable_but_not_retried(self):
        self.use_write_clients(
            lambda call: FakeResponse(
                429,
                {"errors": [{"title": "Rate limited"}]},
                headers={"retry-after": "1"},
            )
        )

        with self.assertRaises(pco_writes.WriteError) as ctx:
            pco_writes.run_write(**self.delete_args(), confirm=True)

        self.assertEqual(len(self.write_requests), 1)
        self.assertEqual(len(self.unw_client.request_calls), 1)
        self.assertIs(ctx.exception.retryable, True)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIs(ctx.exception.outcome_unknown, False)

    def test_5xx_reports_unknown_outcome_without_a_retry(self):
        self.use_write_clients(
            lambda call: FakeResponse(503, {"errors": [{"title": "Unavailable"}]})
        )

        with self.assertRaises(pco_writes.WriteError) as ctx:
            pco_writes.run_write(**self.delete_args(), confirm=True)

        self.assertEqual(len(self.write_requests), 1)
        self.assertIs(ctx.exception.outcome_unknown, True)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("verify", str(ctx.exception).lower())

    def test_transport_error_reports_unknown_outcome_without_a_retry(self):
        def responder(call):
            raise httpx.ConnectError("connection reset")

        self.use_write_clients(responder)

        with self.assertRaises(pco_writes.WriteError) as ctx:
            pco_writes.run_write(**self.delete_args(), confirm=True)

        self.assertEqual(len(self.write_requests), 1)
        self.assertEqual(len(self.unw_client.request_calls), 1)
        self.assertIs(ctx.exception.outcome_unknown, True)
        self.assertIn("verify", str(ctx.exception).lower())

    def test_403_mentions_write_permission(self):
        self.use_write_clients(
            lambda call: FakeResponse(403, {"errors": [{"title": "Forbidden"}]})
        )

        with self.assertRaises(pco_writes.WriteError) as ctx:
            pco_writes.run_write(**self.delete_args(), confirm=True)

        self.assertEqual(len(self.write_requests), 1)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("permission", str(ctx.exception).lower())

    def test_204_delete_has_no_body_and_no_resource(self):
        self.use_write_clients(lambda call: NoContentResponse())

        result = pco_writes.run_write(**self.delete_args(), confirm=True)

        call = self.only_write()
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(call["path"], "/service_types/101/plans/201")
        self.assertIsNone(call.get("json"))
        self.assertEqual(result["mode"], "write")
        self.assertIs(result["written"], True)
        self.assertEqual(result["status_code"], 204)
        self.assertIs(result["deleted"], True)
        self.assertIsNone(result["resource"])

    def test_unallowlisted_paths_and_methods_are_rejected_before_writing(self):
        self.use_write_clients()
        bad_cases = [
            self.delete_args(path="/organizations/1"),
            self.delete_args(path="/service_types/101/plans/201/items"),
            self.delete_args(method="PUT"),
            self.delete_args(method="GET"),
        ]
        for args in bad_cases:
            with self.subTest(args=args):
                with self.assertRaises(pco_writes.WriteError):
                    pco_writes.run_write(**args, confirm=True)
        self.assert_no_writes()

    def test_non_boolean_confirm_is_rejected(self):
        self.use_write_clients()
        with self.assertRaises(pco_writes.WriteError):
            pco_writes.run_write(**self.delete_args(), confirm="yes")
        self.assert_no_writes()


if __name__ == "__main__":
    unittest.main()

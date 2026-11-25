"""server.py tool tests: default routing, fan-out tagging, plan detail, history.

No network access. pco_accounts.get_client (and the copy server.py imported)
is replaced with fake clients that answer canned PCO payloads. Credential and
config file paths point at temporary files; the request pacing sleeps are
disabled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

import pco_accounts as pa
import pco_writes
import server


CREDS = {
    "main": {"appId": "main-id", "appSecret": "main-secret", "orgName": "Main Campus"},
    "north": {"appId": "north-id", "appSecret": "north-secret", "orgName": "North Campus"},
}

PLAN_COUNTS = {
    "service_time_count": 2,
    "rehearsal_time_count": 1,
    "other_time_count": 0,
    "plan_people_count": 5,
    "items_count": 9,
    "total_length": 3600,
    "multi_day": False,
}

PLAN_TIMES_PAYLOAD = {
    "data": [
        {
            "id": "t-service",
            "type": "PlanTime",
            "attributes": {
                "name": "Service",
                "time_type": "service",
                "starts_at": "2026-11-22T10:30:00Z",
                "ends_at": "2026-11-22T12:00:00Z",
            },
        },
        {
            "id": "t-rehearsal",
            "type": "PlanTime",
            "attributes": {
                "name": "Rehearsal",
                "time_type": "rehearsal",
                "starts_at": "2026-11-22T09:00:00Z",
                "ends_at": "2026-11-22T10:00:00Z",
            },
        },
    ]
}

ITEMS_PAYLOAD = {
    "data": [
        {
            "id": "i1",
            "type": "Item",
            "attributes": {
                "item_type": "header",
                "title": "Opening",
                "sequence": 1,
                "description": None,
            },
        },
        {
            "id": "i2",
            "type": "Item",
            "attributes": {
                "item_type": "song",
                "title": "Great Are You Lord",
                "sequence": 2,
                "key_name": "G",
                "description": None,
            },
            "relationships": {
                "song": {"data": {"id": "s1", "type": "Song"}},
                "arrangement": {"data": {"id": "a1", "type": "Arrangement"}},
                "key": {"data": {"id": "k1", "type": "Key"}},
            },
        },
    ],
    "included": [
        {
            "id": "s1",
            "type": "Song",
            "attributes": {
                "title": "Great Are You Lord",
                "author": "All Sons & Daughters",
                "ccli_number": "5074811",
            },
        },
        {
            "id": "a1",
            "type": "Arrangement",
            "attributes": {
                "name": "Default",
                "bpm": 72,
                "meter": "4/4",
                "default_key": "G",
                "notes": None,
            },
        },
        {"id": "k1", "type": "Key", "attributes": {"name": "G", "starting_key": "G"}},
    ],
}

TEAM_MEMBERS_PAYLOAD = {
    "data": [
        {
            "id": "tm1",
            "type": "TeamMember",
            "attributes": {
                "status": "D",
                "decline_reason": "Sick",
                "status_updated_at": "2026-11-15T12:00:00Z",
                "team_position_name": "Acoustic Guitar",
                "name": "Alex Morgan",
            },
            "relationships": {"person": {"data": {"id": "9", "type": "Person"}}},
        }
    ]
}

PEOPLE_PAYLOAD = {
    "data": [
        {
            "id": "9",
            "type": "Person",
            "attributes": {"full_name": "Alex Morgan", "archived": False},
        }
    ]
}

SEARCH_SONGS_PAYLOAD = {
    "data": [
        {
            "id": "s1",
            "type": "Song",
            "attributes": {
                "title": "Great Are You Lord",
                "author": "All Sons & Daughters",
                "ccli_number": "5074811",
            },
        }
    ]
}

SONG_PAYLOAD = {
    "data": {
        "id": "s1",
        "type": "Song",
        "attributes": {
            "title": "Great Are You Lord",
            "author": "All Sons & Daughters",
            "ccli_number": "5074811",
            "copyright": "2013 Integrity",
        },
    },
    "included": [
        {
            "id": "a1",
            "type": "Arrangement",
            "attributes": {
                "name": "Default",
                "bpm": 72,
                "meter": "4/4",
                "default_key": "G",
                "notes": None,
            },
        }
    ],
}


def _path_only(path: str) -> str:
    text = str(path).split("?", 1)[0]
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = {"data": []} if payload is None else payload
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request(
                "GET", "https://api.planningcenteronline.com/services/v2/test"
            )
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )


def ok(payload):
    return FakeResponse(200, payload)


class FakeClient:
    def __init__(self, handler, writer=None):
        self.handler = handler
        self.writer = writer
        self.calls: list[tuple[str, dict]] = []
        self.request_calls: list[dict] = []

    def get(self, path, params=None, **kwargs):
        captured = dict(params or {})
        self.calls.append((str(path), captured))
        return self.handler(str(path), captured)

    def request(self, method, path=None, *, url=None, **kwargs):
        """Record a mutating request; refuse one unless a writer was supplied.

        httpx accepts the target as ``url``; pco_writes passes it positionally.
        """
        target = path if path is not None else url
        if target is None:
            raise TypeError("FakeClient.request requires a path or url")
        record = {"method": str(method).upper(), "path": str(target)}
        record.update(kwargs)
        self.request_calls.append(record)
        if self.writer is None:
            raise AssertionError(
                f"unexpected mutating request {record['method']} {record['path']}"
            )
        return self.writer(record)

    def close(self):
        pass


def _plan_attributes(account: str) -> dict:
    attrs = {
        "title": f"{account} Sunday",
        "dates": "November 22, 2026",
        "sort_date": "2026-11-22",
    }
    attrs.update(PLAN_COUNTS)
    return attrs


def org_handler(account: str, org: str, st_id: str, plan_id: str, plan_times_status=200):
    """Canned PCO responses for one organization; 404 for paths it does not own."""
    st = {"id": st_id, "type": "ServiceType", "attributes": {"name": "Sunday Morning", "frequency": "Weekly"}}
    plan = {"id": plan_id, "type": "Plan", "attributes": _plan_attributes(account)}

    def handler(path, params):
        p = _path_only(path)
        if p == "/service_types":
            return ok({"data": [st]})
        if p == f"/service_types/{st_id}":
            return ok({"data": st})
        if p == f"/service_types/{st_id}/plans":
            return ok({"data": [plan]})
        if p == f"/service_types/{st_id}/plans/{plan_id}":
            return ok({"data": plan})
        if p == f"/service_types/{st_id}/plans/{plan_id}/plan_times":
            if plan_times_status != 200:
                return FakeResponse(plan_times_status, {"errors": [{"title": "not found"}]})
            return ok(PLAN_TIMES_PAYLOAD)
        if p == f"/service_types/{st_id}/plans/{plan_id}/items":
            return ok(ITEMS_PAYLOAD)
        if p == f"/service_types/{st_id}/plans/{plan_id}/team_members":
            return ok(TEAM_MEMBERS_PAYLOAD)
        if p == "/people":
            return ok(PEOPLE_PAYLOAD)
        if p == "/songs":
            return ok(SEARCH_SONGS_PAYLOAD)
        if p == "/songs/s1":
            return ok(SONG_PAYLOAD)
        return FakeResponse(404, {"errors": [{"title": f"no route {p}"}]})

    return handler


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.creds_path = root / "creds.json"
        self.config_path = root / "config.json"

        self._patches = []
        for module in (pa, server):
            for name, value in (
                ("CREDS_FILE", self.creds_path),
                ("CONFIG_FILE", self.config_path),
            ):
                if hasattr(module, name):
                    patcher = mock.patch.object(module, name, value)
                    patcher.start()
                    self._patches.append(patcher)

        self._env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patcher.start()
        for key in [k for k in os.environ if k.startswith("PCO_")]:
            os.environ.pop(key, None)

        # Unit tests are direct stdio: never read/write a real chat runtime file.
        scope_patcher = mock.patch.object(pa, "_chat_scope", return_value=None)
        scope_patcher.start()
        self._patches.append(scope_patcher)

        # Keep the rate-limit pacing (0.25 s/request) out of the suite.
        pace_patcher = mock.patch.object(server, "_pace", lambda: None)
        pace_patcher.start()
        self._patches.append(pace_patcher)

        # Clears process-local selection, the installation snapshot, and caches.
        pa.reset_process_state()
        self.client_map: dict[str, object] = {}
        self.client_calls: list[str] = []
        self.addCleanup(self._teardown)

    def _teardown(self):
        pa.reset_process_state()
        for patcher in reversed(self._patches):
            patcher.stop()
        self._env_patcher.stop()

    def write_creds(self, data):
        self.creds_path.write_text(json.dumps(data), encoding="utf-8")

    def write_config(self, data):
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

    def _fake_get_client(self, account):
        self.client_calls.append(account)
        value = self.client_map.get(account)
        if value is None:
            raise pa.UnknownAccountError(f"Unknown PCO account '{account}'.")
        if isinstance(value, BaseException):
            raise value
        return value

    def use_clients(self, mapping):
        """Patch pco_accounts.get_client plus every server/pco_writes alias of it."""
        self.client_map = dict(mapping)
        original = pa.get_client
        fake = self._fake_get_client
        patchers = [mock.patch.object(pa, "get_client", fake)]
        for module in (server, pco_writes):
            for name, value in list(vars(module).items()):
                if value is original:
                    patchers.append(mock.patch.object(module, name, fake))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


class AccountRoutingTests(ServerTestBase):
    def test_omitted_account_uses_first_creds_key(self):
        self.write_creds(CREDS)
        main = FakeClient(org_handler("main", "Main Campus", "st-main", "p-main"))
        north = FakeClient(org_handler("north", "North Campus", "st-north", "p-north"))
        self.use_clients({"main": main, "north": north})

        items = server.pco_list_service_types()

        self.assertEqual(self.client_calls, ["main"])
        self.assertEqual(north.calls, [])
        self.assertEqual(items[0]["account"], "main")
        self.assertEqual(items[0]["org_name"], "Main Campus")

    def test_omitted_account_uses_config_default(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "north"})
        main = FakeClient(org_handler("main", "Main Campus", "st-main", "p-main"))
        north = FakeClient(org_handler("north", "North Campus", "st-north", "p-north"))
        self.use_clients({"main": main, "north": north})

        items = server.pco_list_service_types()

        self.assertEqual(self.client_calls, ["north"])
        self.assertEqual(main.calls, [])
        self.assertEqual(items[0]["account"], "north")

    def test_env_default_beats_config_at_tool_level(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "main"})
        os.environ["PCO_DEFAULT_ACCOUNT"] = "north"
        main = FakeClient(org_handler("main", "Main Campus", "st-main", "p-main"))
        north = FakeClient(org_handler("north", "North Campus", "st-north", "p-north"))
        self.use_clients({"main": main, "north": north})

        items = server.pco_list_service_types()

        self.assertEqual(self.client_calls, ["north"])
        self.assertEqual(items[0]["account"], "north")

    def test_explicit_account_routes_to_that_client(self):
        self.write_creds(CREDS)
        main = FakeClient(org_handler("main", "Main Campus", "st-main", "p-main"))
        north = FakeClient(org_handler("north", "North Campus", "st-north", "p-north"))
        self.use_clients({"main": main, "north": north})

        items = server.pco_list_service_types(account="north")

        self.assertEqual(self.client_calls, ["north"])
        self.assertEqual(main.calls, [])
        self.assertEqual(items[0]["org_name"], "North Campus")

    def test_unknown_explicit_account_raises_without_building_client(self):
        self.write_creds(CREDS)
        bogus = FakeClient(org_handler("bogus", "Bogus Org", "st-bogus", "p-bogus"))
        self.use_clients({"main": FakeClient(lambda *_: ok({"data": []})), "bogus": bogus})

        with self.assertRaises(pa.AccountError):
            server.pco_list_service_types(account="bogus")

        self.assertEqual(bogus.calls, [])
        self.assertEqual(self.client_calls, [])

    def test_list_accounts_detailed_marks_default(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "north"})

        self.assertEqual(server.pco_list_accounts(), ["main", "north"])
        detailed = server.pco_list_accounts(detailed=True)

        self.assertEqual({row["account"] for row in detailed}, {"main", "north"})
        defaults = [row for row in detailed if row["default"]]
        self.assertEqual([row["account"] for row in defaults], ["north"])
        by_account = {row["account"]: row for row in detailed}
        self.assertEqual(by_account["north"]["org_name"], "North Campus")
        self.assertTrue(all(row["has_credentials"] for row in detailed))
        self.assertFalse(any(row["selected"] for row in detailed))
        self.assertEqual(
            [row["account"] for row in detailed if row["effective"]], ["north"]
        )

    def test_set_default_account_persists_and_rejects_all(self):
        self.write_creds(CREDS)

        result = server.pco_set_default_account("north", persist=True)

        self.assertEqual(result["default_account"], "north")
        self.assertEqual(result["scope"], "persisted")
        self.assertEqual(result["config_file"], str(self.config_path))
        self.assertEqual(result["accounts"], ["main", "north"])
        self.assertEqual(pa.selected_account(), "north")
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["defaultAccount"], "north")
        self.assertEqual(pa.default_account(), "north")

        with self.assertRaises((pa.AccountError, ValueError)):
            server.pco_set_default_account("all", persist=True)
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["defaultAccount"], "north")


class FanOutTests(ServerTestBase):
    def test_account_all_tags_every_plan(self):
        self.write_creds(CREDS)
        main = FakeClient(org_handler("main", "Main Campus", "st-main", "p-main"))
        north = FakeClient(org_handler("north", "North Campus", "st-north", "p-north"))
        self.use_clients({"main": main, "north": north})

        envelope = server.pco_list_plans(account="all")

        self.assertEqual(envelope["account"], "all")
        self.assertCountEqual(envelope["accounts"], ["main", "north"])
        self.assertEqual(envelope["errors"], [])
        by_account = {row["account"]: row for row in envelope["results"]}
        self.assertEqual(set(by_account), {"main", "north"})
        for account, row in by_account.items():
            expected_org = "Main Campus" if account == "main" else "North Campus"
            self.assertEqual(row["org_name"], expected_org)
            self.assertTrue(row["data"])
            for plan in row["data"]:
                self.assertEqual(plan["account"], account)
                self.assertEqual(plan["org_name"], expected_org)
                self.assertEqual(plan["service_type_name"], "Sunday Morning")

    def test_account_all_is_case_insensitive(self):
        self.write_creds(CREDS)
        self.use_clients({
            "main": FakeClient(org_handler("main", "Main Campus", "st-main", "p-main")),
            "north": FakeClient(org_handler("north", "North Campus", "st-north", "p-north")),
        })

        envelope = server.pco_list_service_types(account="ALL")

        self.assertEqual(envelope["account"], "all")
        self.assertEqual(len(envelope["results"]), 2)
        for row in envelope["results"]:
            self.assertTrue(row["data"])
            for item in row["data"]:
                self.assertEqual(item["account"], row["account"])
                self.assertEqual(item["org_name"], row["org_name"])

    def test_partial_failure_keeps_working_account(self):
        self.write_creds(CREDS)
        main = FakeClient(org_handler("main", "Main Campus", "st-main", "p-main"))
        self.use_clients({
            "main": main,
            "north": pa.AccountError("401 unauthorized for north"),
        })

        envelope = server.pco_list_plans(account="all")

        self.assertEqual([row["account"] for row in envelope["results"]], ["main"])
        self.assertTrue(envelope["results"][0]["data"])
        self.assertEqual(len(envelope["errors"]), 1)
        error = envelope["errors"][0]
        self.assertEqual(error["account"], "north")
        self.assertEqual(error["org_name"], "North Campus")
        self.assertIn("401", str(error["error"]))

    def test_all_accounts_fail_raises(self):
        self.write_creds(CREDS)
        self.use_clients({
            "main": pa.AccountError("main down"),
            "north": pa.AccountError("north down"),
        })

        with self.assertRaises(RuntimeError) as ctx:
            server.pco_list_plans(account="all")

        message = str(ctx.exception)
        self.assertIn("main down", message)
        self.assertIn("north down", message)


class PlanDetailTests(ServerTestBase):
    def test_get_plan_includes_plan_times_counts_and_tags(self):
        self.write_creds(CREDS)
        client = FakeClient(org_handler("main", "Main Campus", "st-main", "7777777"))
        self.use_clients({"main": client})

        result = server.pco_get_plan("7777777", account="main", include_attachments=False)

        self.assertEqual(result["account"], "main")
        self.assertEqual(result["org_name"], "Main Campus")
        self.assertEqual(result["plan_id"], "7777777")

        self.assertEqual([t["id"] for t in result["plan_times"]], ["t-rehearsal", "t-service"])
        for time in result["plan_times"]:
            self.assertEqual(
                set(time), {"id", "name", "time_type", "starts_at", "ends_at"}
            )

        for key, value in PLAN_COUNTS.items():
            self.assertEqual(result["counts"][key], value)
        self.assertNotIn("plan_times_error", result)

        self.assertEqual(len(result["items"]), 2)
        for item in result["items"]:
            self.assertEqual(item["account"], "main")
            self.assertEqual(item["org_name"], "Main Campus")

    def test_plan_times_failure_is_non_fatal(self):
        self.write_creds(CREDS)
        client = FakeClient(
            org_handler("main", "Main Campus", "st-main", "7777777", plan_times_status=404)
        )
        self.use_clients({"main": client})

        result = server.pco_get_plan("7777777", account="main", include_attachments=False)

        self.assertEqual(result["plan_times"], [])
        self.assertIn("plan_times_error", result)
        self.assertEqual(result["title"], "main Sunday")

    def test_get_plan_all_accounts_sets_ambiguous(self):
        self.write_creds(CREDS)
        main = FakeClient(org_handler("main", "Main Campus", "st-main", "7777777"))
        north = FakeClient(org_handler("north", "North Campus", "st-north", "7777777"))
        self.use_clients({"main": main, "north": north})

        envelope = server.pco_get_plan("7777777", account="all", include_attachments=False)

        self.assertEqual(envelope["account"], "all")
        self.assertEqual(envelope["account_source"], "all")
        self.assertTrue(envelope["ambiguous"])
        self.assertEqual(envelope["errors"], [])
        self.assertCountEqual(
            [row["account"] for row in envelope["results"]], ["main", "north"]
        )
        for row in envelope["results"]:
            data = row["data"]
            self.assertEqual(data["plan_id"], "7777777")
            self.assertEqual(data["account"], row["account"])
            self.assertEqual(data["org_name"], row["org_name"])
            self.assertEqual(len(data["plan_times"]), 2)
            self.assertEqual(data["counts"]["service_time_count"], 2)


class SongAndHistoryTests(ServerTestBase):
    def test_search_songs_uses_params_and_tags_results(self):
        self.write_creds(CREDS)
        client = FakeClient(org_handler("north", "North Campus", "st-north", "p-north"))
        self.use_clients({"north": client})

        songs = server.pco_search_songs("Great Are You Lord", account="north")

        path, params = client.calls[0]
        self.assertNotIn("Great Are You Lord", path)
        self.assertTrue(any("Great Are You Lord" in str(v) for v in params.values()))
        self.assertEqual(songs[0]["account"], "north")
        self.assertEqual(songs[0]["org_name"], "North Campus")

    def test_get_song_tags_top_level(self):
        self.write_creds(CREDS)
        client = FakeClient(org_handler("main", "Main Campus", "st-main", "p-main"))
        self.use_clients({"main": client})

        song = server.pco_get_song("s1", account="main")

        self.assertEqual(song["account"], "main")
        self.assertEqual(song["org_name"], "Main Campus")
        self.assertEqual(song["title"], "Great Are You Lord")

    def test_person_history_adds_org_tags_and_decline_fields(self):
        self.write_creds(CREDS)
        client = FakeClient(org_handler("main", "Main Campus", "st-main", "5555555"))
        self.use_clients({"main": client})

        result = server.pco_person_song_history(
            account="main",
            person_name="Alex Morgan",
            limit=5,
            include_declined=True,
        )

        self.assertEqual(result["account"], "main")
        self.assertEqual(result["org_name"], "Main Campus")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["returned_plans"], 1)

        plan = result["plans"][0]
        self.assertEqual(plan["account"], "main")
        self.assertEqual(plan["org_name"], "Main Campus")
        assignment = plan["assignments"][0]
        self.assertEqual(assignment["status"], "D")
        self.assertEqual(assignment["decline_reason"], "Sick")
        self.assertEqual(assignment["status_updated_at"], "2026-11-15T12:00:00Z")
        self.assertEqual(plan["songs"][0]["title"], "Great Are You Lord")
        self.assertEqual(plan["songs"][0]["key"], "G")


class DualAccountMixin:
    """Two fake organizations wired into pco_accounts.get_client and server aliases."""

    def use_two_accounts(self):
        self.main_client = FakeClient(
            org_handler("main", "Main Campus", "st-main", "p-main")
        )
        self.north_client = FakeClient(
            org_handler("north", "North Campus", "st-north", "p-north")
        )
        self.use_clients({"main": self.main_client, "north": self.north_client})


class ChatSelectionTests(DualAccountMixin, ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)
        self.use_two_accounts()

    def test_chat_selection_routes_omitted_and_explicit_overrides(self):
        used = server.pco_use_account("north")
        self.assertEqual(used["selected_account"], "north")
        self.assertEqual(used["effective_account"], "north")
        self.assertEqual(used["effective_source"], "chat")
        self.assertIsInstance(used["changed"], bool)
        self.assertTrue(used["changed"])
        self.assertIsInstance(used["message"], str)
        self.assertTrue(used["message"])

        # Omitted -> this chat's selection.
        self.client_calls.clear()
        rows = server.pco_list_service_types()
        self.assertEqual(self.client_calls, ["north"])
        self.assertEqual(rows[0]["account"], "north")
        self.assertEqual(rows[0]["account_source"], "chat")
        self.assertEqual(self.main_client.calls, [])

        # Explicit beats the selection.
        self.client_calls.clear()
        rows = server.pco_list_service_types(account="main")
        self.assertEqual(self.client_calls, ["main"])
        self.assertEqual(rows[0]["account"], "main")
        self.assertEqual(rows[0]["account_source"], "explicit")

        # "all" spans every configured org regardless of the selection.
        envelope = server.pco_list_service_types(account="all")
        self.assertEqual(envelope["account"], "all")
        self.assertEqual(envelope["account_source"], "all")
        self.assertCountEqual(envelope["accounts"], ["main", "north"])
        self.assertEqual(pa.selected_account(), "north")

    def test_clearing_selection_reverts_to_installation_default(self):
        server.pco_use_account("north")

        cleared = server.pco_use_account(None)

        self.assertIsNone(cleared["selected_account"])
        self.assertEqual(cleared["effective_account"], "main")
        self.assertEqual(cleared["effective_source"], "installation")
        self.assertEqual(cleared["installation_default"], "main")

        self.client_calls.clear()
        rows = server.pco_list_service_types()
        self.assertEqual(self.client_calls, ["main"])
        self.assertEqual(rows[0]["account"], "main")
        self.assertEqual(rows[0]["account_source"], "installation")

    def test_use_account_never_writes_config(self):
        server.pco_use_account("north")
        self.assertFalse(self.config_path.exists())
        server.pco_use_account(None)
        self.assertFalse(self.config_path.exists())


class AccountStatusTests(ServerTestBase):
    STATUS_KEYS = {
        "instance_id",
        "pid",
        "scope",
        "scope_id",
        "installation_default",
        "installation_default_source",
        "selected_account",
        "effective_account",
        "effective_source",
        "routing_warning",
        "accounts",
    }
    ROW_KEYS = {
        "account",
        "org_name",
        "default",
        "selected",
        "effective",
        "has_credentials",
    }

    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)

    def test_status_shape_flags_and_warning(self):
        status = server.pco_account_status()

        self.assertEqual(set(status), self.STATUS_KEYS)
        self.assertRegex(status["instance_id"], r"^[0-9a-f]{12}$")
        self.assertEqual(
            status["instance_id"], server.pco_account_status()["instance_id"]
        )
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(status["scope"], "process")
        self.assertIsNone(status["scope_id"])
        self.assertEqual(status["installation_default"], "main")
        self.assertEqual(status["installation_default_source"], "first-key")
        self.assertIsNone(status["selected_account"])
        self.assertEqual(status["effective_account"], "main")
        self.assertEqual(status["effective_source"], "installation")
        self.assertIsInstance(status["routing_warning"], str)
        self.assertTrue(status["routing_warning"])

        rows = {row["account"]: row for row in status["accounts"]}
        self.assertEqual(set(rows), {"main", "north"})
        for row in rows.values():
            self.assertEqual(set(row), self.ROW_KEYS)
            self.assertTrue(row["has_credentials"])
        self.assertTrue(rows["main"]["default"])
        self.assertTrue(rows["main"]["effective"])
        self.assertFalse(rows["north"]["default"])
        self.assertFalse(rows["north"]["effective"])
        self.assertFalse(any(row["selected"] for row in rows.values()))

        server.pco_use_account("north")
        selected = server.pco_account_status()

        self.assertIsNone(selected["routing_warning"])
        self.assertEqual(selected["selected_account"], "north")
        self.assertEqual(selected["effective_account"], "north")
        self.assertEqual(selected["effective_source"], "chat")
        rows = {row["account"]: row for row in selected["accounts"]}
        self.assertTrue(rows["north"]["selected"])
        self.assertTrue(rows["north"]["effective"])
        self.assertFalse(rows["north"]["default"])
        self.assertTrue(rows["main"]["default"])
        self.assertFalse(rows["main"]["selected"])
        self.assertFalse(rows["main"]["effective"])

    def test_single_account_has_no_warning(self):
        self.write_creds({"main": CREDS["main"]})
        status = server.pco_account_status()
        self.assertIsNone(status["routing_warning"])
        self.assertEqual([row["account"] for row in status["accounts"]], ["main"])
        self.assertTrue(status["accounts"][0]["default"])

    def test_no_accounts_reports_builtin_default(self):
        self.creds_path.unlink()
        status = server.pco_account_status()
        self.assertEqual(status["accounts"], [])
        self.assertEqual(
            status["installation_default"], pa.DEFAULT_ACCOUNT_FALLBACK
        )
        self.assertEqual(status["installation_default_source"], "builtin")
        self.assertEqual(status["effective_source"], "installation")
        self.assertIn("No PCO accounts are configured", status["routing_warning"])

    def test_chat_scope_is_reported(self):
        self.write_creds(CREDS)
        with mock.patch.object(server, "chat_scope_id", return_value="4242:777"):
            status = server.pco_account_status()

        self.assertEqual(status["scope"], "chat")
        self.assertEqual(status["scope_id"], "4242:777")


class PersistTests(DualAccountMixin, ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)
        self.use_two_accounts()

    def test_persist_false_routes_chat_only(self):
        server.pco_account_status()  # freeze the installation snapshot at main

        result = server.pco_set_default_account("north")

        self.assertEqual(result["default_account"], "north")
        self.assertEqual(result["scope"], "chat")
        self.assertIsNone(result["config_file"])
        self.assertFalse(self.config_path.exists())
        self.assertEqual(result["accounts"], ["main", "north"])
        self.assertEqual(pa.selected_account(), "north")

        status = result["status"]
        self.assertEqual(status["selected_account"], "north")
        self.assertEqual(status["effective_account"], "north")
        self.assertEqual(status["effective_source"], "chat")
        self.assertEqual(status["installation_default"], "main")

        self.client_calls.clear()
        self.assertEqual(server.pco_list_service_types()[0]["account"], "north")

    def test_persist_true_writes_config_for_future_chats_only(self):
        server.pco_account_status()  # freeze the installation snapshot at main

        result = server.pco_set_default_account("north", persist=True)

        self.assertEqual(result["scope"], "persisted")
        self.assertEqual(result["config_file"], str(self.config_path))
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["defaultAccount"], "north")
        # This process keeps its snapshot; only NEW chats read the file.
        self.assertEqual(result["status"]["installation_default"], "main")
        self.assertEqual(result["status"]["selected_account"], "north")
        # Dynamic resolution is what a fresh process would snapshot.
        self.assertEqual(pa.default_account(), "north")

    def test_persist_false_does_not_touch_an_existing_config(self):
        self.write_config({"defaultAccount": "main", "orgNames": {"north": "North Campus"}})
        before = self.config_path.read_text(encoding="utf-8")

        server.pco_set_default_account("north")

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertEqual(pa.default_account(), "main")
        self.assertEqual(pa.selected_account(), "north")

    def test_rejects_all_unknown_and_broken_credentials(self):
        with self.assertRaises((pa.AccountError, ValueError)):
            server.pco_set_default_account("all")
        with self.assertRaises(pa.AccountError):
            server.pco_set_default_account("bogus")

        self.write_creds({"main": CREDS["main"], "broken": {"appId": "only-id"}})
        with self.assertRaises(pa.AccountError):
            server.pco_set_default_account("broken", persist=True)

        self.assertFalse(self.config_path.exists())
        self.assertIsNone(pa.selected_account())


class InstallationSnapshotServerTests(DualAccountMixin, ServerTestBase):
    def test_status_and_routing_use_snapshot_until_reset(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "main"})
        first = server.pco_account_status()
        self.assertEqual(first["installation_default"], "main")
        self.assertEqual(first["installation_default_source"], "config")

        # Changing the file must not move this process's default.
        self.write_config({"defaultAccount": "north"})
        second = server.pco_account_status()
        self.assertEqual(second["installation_default"], "main")
        self.assertEqual(second["effective_account"], "main")
        self.assertEqual(second["installation_default_source"], "config")

        pa.reset_process_state()
        third = server.pco_account_status()
        self.assertEqual(third["installation_default"], "north")
        self.assertEqual(third["effective_account"], "north")
        self.assertEqual(third["effective_source"], "installation")

    def test_omitted_call_routes_by_snapshot(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "main"})
        self.use_two_accounts()
        server.pco_account_status()  # freeze at main

        self.write_config({"defaultAccount": "north"})
        self.client_calls.clear()
        rows = server.pco_list_service_types()

        self.assertEqual(self.client_calls, ["main"])
        self.assertEqual(rows[0]["account"], "main")
        self.assertEqual(rows[0]["account_source"], "installation")


class DetailedListingSnapshotTests(ServerTestBase):
    def test_listing_default_uses_snapshot_not_live_config(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "main"})
        server.pco_account_status()  # freeze at main
        self.write_config({"defaultAccount": "north"})

        rows = {
            row["account"]: row for row in server.pco_list_accounts(detailed=True)
        }
        self.assertEqual(set(rows), {"main", "north"})
        self.assertTrue(rows["main"]["default"])
        self.assertFalse(rows["north"]["default"])
        self.assertTrue(rows["main"]["effective"])
        self.assertFalse(rows["north"]["effective"])
        self.assertFalse(any(row["selected"] for row in rows.values()))
        self.assertTrue(all(row["has_credentials"] for row in rows.values()))

        server.pco_use_account("north")
        rows = {
            row["account"]: row for row in server.pco_list_accounts(detailed=True)
        }
        self.assertTrue(rows["main"]["default"])
        self.assertFalse(rows["north"]["default"])
        self.assertTrue(rows["north"]["selected"])
        self.assertTrue(rows["north"]["effective"])
        self.assertFalse(rows["main"]["effective"])

    def test_flat_listing_unchanged(self):
        self.write_creds(CREDS)
        self.assertEqual(server.pco_list_accounts(), ["main", "north"])


class AccountSourceTests(DualAccountMixin, ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)
        self.use_two_accounts()

    def test_dict_tool_reports_source(self):
        song = server.pco_get_song("s1")
        self.assertEqual(song["account"], "main")
        self.assertEqual(song["account_source"], "installation")

        server.pco_use_account("north")
        song = server.pco_get_song("s1")
        self.assertEqual(song["account"], "north")
        self.assertEqual(song["account_source"], "chat")

        song = server.pco_get_song("s1", account="main")
        self.assertEqual(song["account"], "main")
        self.assertEqual(song["account_source"], "explicit")

    def test_list_rows_report_source(self):
        rows = server.pco_list_service_types()
        self.assertEqual({row["account_source"] for row in rows}, {"installation"})

        server.pco_use_account("north")
        rows = server.pco_list_service_types()
        self.assertEqual({row["account_source"] for row in rows}, {"chat"})

        rows = server.pco_list_service_types(account="main")
        self.assertEqual({row["account_source"] for row in rows}, {"explicit"})

    def test_fan_out_envelope_reports_all(self):
        envelope = server.pco_list_plans(account="all")
        self.assertEqual(envelope["account_source"], "all")


class RemovedSelectionTests(DualAccountMixin, ServerTestBase):
    def test_removed_selected_account_raises_actionable_error(self):
        self.write_creds(CREDS)
        self.use_two_accounts()
        server.pco_use_account("north")

        self.write_creds({"main": CREDS["main"]})

        self.client_calls.clear()
        with self.assertRaises(pa.AccountError) as ctx:
            server.pco_list_service_types()

        message = str(ctx.exception)
        self.assertIn("north", message)
        self.assertIn("pco_use_account", message)
        self.assertEqual(self.client_calls, [])


class ConcurrencyTests(ServerTestBase):
    def test_parallel_selection_and_status_calls(self):
        self.write_creds(CREDS)
        choices = ["north", "main"] * 4
        barrier = threading.Barrier(len(choices))
        errors: list[BaseException] = []

        def worker(name):
            try:
                barrier.wait(timeout=20)
                for _ in range(25):
                    pa.select_account(name)
                    current = pa.selected_account()
                    if current not in ("main", "north"):
                        raise AssertionError(f"invalid selection {current!r}")
                    status = server.pco_account_status()
                    if status["effective_account"] not in ("main", "north"):
                        raise AssertionError(
                            f"invalid effective account {status['effective_account']!r}"
                        )
                    if status["effective_source"] != "chat":
                        raise AssertionError(
                            f"selection did not apply: {status['effective_source']!r}"
                        )
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(name,)) for name in choices
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertIn(pa.selected_account(), {"main", "north"})
        self.assertEqual(
            server.pco_account_status()["selected_account"], pa.selected_account()
        )


ISOLATION_SCRIPT = """
import json
import os
import sys
from pathlib import Path

repo, creds, config, choice = sys.argv[1:5]
sys.path.insert(0, repo)

import pco_accounts as pa

pa.CREDS_FILE = Path(creds)
pa.CONFIG_FILE = Path(config)
pa._chat_scope = lambda: None
pa.reset_process_state()

import server

used = server.pco_use_account(choice)
status = server.pco_account_status()
print("PCO_ISOLATION:" + json.dumps({
    "pid": os.getpid(),
    "use": used,
    "status": status,
}))
"""


class ProcessIsolationTests(ServerTestBase):
    def test_two_processes_keep_independent_selections(self):
        self.write_creds(CREDS)
        repo = str(Path(server.__file__).resolve().parent)
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PCO_")
        }
        choices = ("north", "main")

        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    ISOLATION_SCRIPT,
                    repo,
                    str(self.creds_path),
                    str(self.config_path),
                    choice,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=clean_env,
            )
            for choice in choices
        ]

        payloads = []
        for proc, choice in zip(procs, choices):
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, f"{choice}: {err[-2000:]}")
            line = next(
                (
                    candidate
                    for candidate in out.splitlines()
                    if candidate.startswith("PCO_ISOLATION:")
                ),
                None,
            )
            self.assertIsNotNone(
                line,
                f"{choice} produced no status: {out[-2000:]}{err[-2000:]}",
            )
            payloads.append(json.loads(line[len("PCO_ISOLATION:"):]))

        first, second = payloads
        for payload in payloads:
            self.assertEqual(payload["status"]["pid"], payload["pid"])
            self.assertRegex(payload["status"]["instance_id"], r"^[0-9a-f]{12}$")
            self.assertEqual(payload["status"]["installation_default"], "main")
            self.assertEqual(payload["status"]["scope"], "process")
            self.assertEqual(payload["status"]["effective_source"], "chat")

        self.assertNotEqual(first["pid"], second["pid"])
        self.assertNotEqual(
            first["status"]["instance_id"], second["status"]["instance_id"]
        )
        self.assertEqual(first["status"]["selected_account"], "north")
        self.assertEqual(first["status"]["effective_account"], "north")
        self.assertEqual(second["status"]["selected_account"], "main")
        self.assertEqual(second["status"]["effective_account"], "main")

        # Chat selection must never persist to the shared config file.
        self.assertFalse(self.config_path.exists())


if __name__ == "__main__":
    unittest.main()

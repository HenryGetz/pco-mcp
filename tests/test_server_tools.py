"""server.py tool tests: default routing, fan-out tagging, plan detail, history.

No network access. pco_accounts.get_client (and the copy server.py imported)
is replaced with fake clients that answer canned PCO payloads. Credential and
config file paths point at temporary files; the request pacing sleeps are
disabled.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

import pco_accounts as pa
import server

ENV_KEYS = ("PCO_APP_ID", "PCO_SECRET", "PCO_APP_SECRET", "PCO_DEFAULT_ACCOUNT")

CREDS = {
    "unw": {"appId": "unw-id", "appSecret": "unw-secret", "orgName": "Main Campus"},
    "rs": {"appId": "rs-id", "appSecret": "rs-secret", "orgName": "North Campus"},
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
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None, **kwargs):
        captured = dict(params or {})
        self.calls.append((str(path), captured))
        return self.handler(str(path), captured)

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
        for key in ENV_KEYS:
            os.environ.pop(key, None)

        # Keep the rate-limit pacing (0.25 s/request) out of the suite.
        pace_patcher = mock.patch.object(server, "_pace", lambda: None)
        pace_patcher.start()
        self._patches.append(pace_patcher)

        pa.reset_cache()
        self.client_map: dict[str, object] = {}
        self.client_calls: list[str] = []
        self.addCleanup(self._teardown)

    def _teardown(self):
        pa.reset_cache()
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
        """Patch pco_accounts.get_client plus every server.py alias of it."""
        self.client_map = dict(mapping)
        original = pa.get_client
        fake = self._fake_get_client
        patchers = [mock.patch.object(pa, "get_client", fake)]
        for name, value in list(vars(server).items()):
            if value is original:
                patchers.append(mock.patch.object(server, name, fake))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


class AccountRoutingTests(ServerTestBase):
    def test_omitted_account_uses_first_creds_key(self):
        self.write_creds(CREDS)
        unw = FakeClient(org_handler("unw", "Main Campus", "st-unw", "p-unw"))
        rs = FakeClient(org_handler("rs", "North Campus", "st-rs", "p-rs"))
        self.use_clients({"unw": unw, "rs": rs})

        items = server.pco_list_service_types()

        self.assertEqual(self.client_calls, ["unw"])
        self.assertEqual(rs.calls, [])
        self.assertEqual(items[0]["account"], "unw")
        self.assertEqual(items[0]["org_name"], "Main Campus")

    def test_omitted_account_uses_config_default(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "rs"})
        unw = FakeClient(org_handler("unw", "Main Campus", "st-unw", "p-unw"))
        rs = FakeClient(org_handler("rs", "North Campus", "st-rs", "p-rs"))
        self.use_clients({"unw": unw, "rs": rs})

        items = server.pco_list_service_types()

        self.assertEqual(self.client_calls, ["rs"])
        self.assertEqual(unw.calls, [])
        self.assertEqual(items[0]["account"], "rs")

    def test_env_default_beats_config_at_tool_level(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "unw"})
        os.environ["PCO_DEFAULT_ACCOUNT"] = "rs"
        unw = FakeClient(org_handler("unw", "Main Campus", "st-unw", "p-unw"))
        rs = FakeClient(org_handler("rs", "North Campus", "st-rs", "p-rs"))
        self.use_clients({"unw": unw, "rs": rs})

        items = server.pco_list_service_types()

        self.assertEqual(self.client_calls, ["rs"])
        self.assertEqual(items[0]["account"], "rs")

    def test_explicit_account_routes_to_that_client(self):
        self.write_creds(CREDS)
        unw = FakeClient(org_handler("unw", "Main Campus", "st-unw", "p-unw"))
        rs = FakeClient(org_handler("rs", "North Campus", "st-rs", "p-rs"))
        self.use_clients({"unw": unw, "rs": rs})

        items = server.pco_list_service_types(account="rs")

        self.assertEqual(self.client_calls, ["rs"])
        self.assertEqual(unw.calls, [])
        self.assertEqual(items[0]["org_name"], "North Campus")

    def test_unknown_explicit_account_raises_without_building_client(self):
        self.write_creds(CREDS)
        bogus = FakeClient(org_handler("bogus", "Bogus Org", "st-bogus", "p-bogus"))
        self.use_clients({"unw": FakeClient(lambda *_: ok({"data": []})), "bogus": bogus})

        with self.assertRaises(pa.AccountError):
            server.pco_list_service_types(account="bogus")

        self.assertEqual(bogus.calls, [])
        self.assertEqual(self.client_calls, [])

    def test_list_accounts_detailed_marks_default(self):
        self.write_creds(CREDS)
        self.write_config({"defaultAccount": "rs"})

        self.assertEqual(server.pco_list_accounts(), ["unw", "rs"])
        detailed = server.pco_list_accounts(detailed=True)

        self.assertEqual({row["account"] for row in detailed}, {"unw", "rs"})
        defaults = [row for row in detailed if row["default"]]
        self.assertEqual([row["account"] for row in defaults], ["rs"])
        by_account = {row["account"]: row for row in detailed}
        self.assertEqual(by_account["rs"]["org_name"], "North Campus")
        self.assertTrue(all(row["has_credentials"] for row in detailed))

    def test_set_default_account_persists_and_rejects_all(self):
        self.write_creds(CREDS)

        result = server.pco_set_default_account("rs")

        self.assertEqual(result["default_account"], "rs")
        self.assertEqual(result["config_file"], str(self.config_path))
        self.assertEqual(result["accounts"], ["unw", "rs"])
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["defaultAccount"], "rs")
        self.assertEqual(pa.default_account(), "rs")

        with self.assertRaises((pa.AccountError, ValueError)):
            server.pco_set_default_account("all")
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["defaultAccount"], "rs")


class FanOutTests(ServerTestBase):
    def test_account_all_tags_every_plan(self):
        self.write_creds(CREDS)
        unw = FakeClient(org_handler("unw", "Main Campus", "st-unw", "p-unw"))
        rs = FakeClient(org_handler("rs", "North Campus", "st-rs", "p-rs"))
        self.use_clients({"unw": unw, "rs": rs})

        envelope = server.pco_list_plans(account="all")

        self.assertEqual(envelope["account"], "all")
        self.assertCountEqual(envelope["accounts"], ["unw", "rs"])
        self.assertEqual(envelope["errors"], [])
        by_account = {row["account"]: row for row in envelope["results"]}
        self.assertEqual(set(by_account), {"unw", "rs"})
        for account, row in by_account.items():
            expected_org = "Main Campus" if account == "unw" else "North Campus"
            self.assertEqual(row["org_name"], expected_org)
            self.assertTrue(row["data"])
            for plan in row["data"]:
                self.assertEqual(plan["account"], account)
                self.assertEqual(plan["org_name"], expected_org)
                self.assertEqual(plan["service_type_name"], "Sunday Morning")

    def test_account_all_is_case_insensitive(self):
        self.write_creds(CREDS)
        self.use_clients({
            "unw": FakeClient(org_handler("unw", "Main Campus", "st-unw", "p-unw")),
            "rs": FakeClient(org_handler("rs", "North Campus", "st-rs", "p-rs")),
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
        unw = FakeClient(org_handler("unw", "Main Campus", "st-unw", "p-unw"))
        self.use_clients({
            "unw": unw,
            "rs": pa.AccountError("401 unauthorized for rs"),
        })

        envelope = server.pco_list_plans(account="all")

        self.assertEqual([row["account"] for row in envelope["results"]], ["unw"])
        self.assertTrue(envelope["results"][0]["data"])
        self.assertEqual(len(envelope["errors"]), 1)
        error = envelope["errors"][0]
        self.assertEqual(error["account"], "rs")
        self.assertEqual(error["org_name"], "North Campus")
        self.assertIn("401", str(error["error"]))

    def test_all_accounts_fail_raises(self):
        self.write_creds(CREDS)
        self.use_clients({
            "unw": pa.AccountError("unw down"),
            "rs": pa.AccountError("rs down"),
        })

        with self.assertRaises(RuntimeError) as ctx:
            server.pco_list_plans(account="all")

        message = str(ctx.exception)
        self.assertIn("unw down", message)
        self.assertIn("rs down", message)


class PlanDetailTests(ServerTestBase):
    def test_get_plan_includes_plan_times_counts_and_tags(self):
        self.write_creds(CREDS)
        client = FakeClient(org_handler("unw", "Main Campus", "st-unw", "7777777"))
        self.use_clients({"unw": client})

        result = server.pco_get_plan("7777777", account="unw", include_attachments=False)

        self.assertEqual(result["account"], "unw")
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
            self.assertEqual(item["account"], "unw")
            self.assertEqual(item["org_name"], "Main Campus")

    def test_plan_times_failure_is_non_fatal(self):
        self.write_creds(CREDS)
        client = FakeClient(
            org_handler("unw", "Main Campus", "st-unw", "7777777", plan_times_status=404)
        )
        self.use_clients({"unw": client})

        result = server.pco_get_plan("7777777", account="unw", include_attachments=False)

        self.assertEqual(result["plan_times"], [])
        self.assertIn("plan_times_error", result)
        self.assertEqual(result["title"], "unw Sunday")

    def test_get_plan_all_accounts_sets_ambiguous(self):
        self.write_creds(CREDS)
        unw = FakeClient(org_handler("unw", "Main Campus", "st-unw", "7777777"))
        rs = FakeClient(org_handler("rs", "North Campus", "st-rs", "7777777"))
        self.use_clients({"unw": unw, "rs": rs})

        envelope = server.pco_get_plan("7777777", account="all", include_attachments=False)

        self.assertEqual(envelope["account"], "all")
        self.assertTrue(envelope["ambiguous"])
        self.assertEqual(envelope["errors"], [])
        self.assertCountEqual(
            [row["account"] for row in envelope["results"]], ["unw", "rs"]
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
        client = FakeClient(org_handler("rs", "North Campus", "st-rs", "p-rs"))
        self.use_clients({"rs": client})

        songs = server.pco_search_songs("Great Are You Lord", account="rs")

        path, params = client.calls[0]
        self.assertNotIn("Great Are You Lord", path)
        self.assertTrue(any("Great Are You Lord" in str(v) for v in params.values()))
        self.assertEqual(songs[0]["account"], "rs")
        self.assertEqual(songs[0]["org_name"], "North Campus")

    def test_get_song_tags_top_level(self):
        self.write_creds(CREDS)
        client = FakeClient(org_handler("unw", "Main Campus", "st-unw", "p-unw"))
        self.use_clients({"unw": client})

        song = server.pco_get_song("s1", account="unw")

        self.assertEqual(song["account"], "unw")
        self.assertEqual(song["org_name"], "Main Campus")
        self.assertEqual(song["title"], "Great Are You Lord")

    def test_person_history_adds_org_tags_and_decline_fields(self):
        self.write_creds(CREDS)
        client = FakeClient(org_handler("unw", "Main Campus", "st-unw", "5555555"))
        self.use_clients({"unw": client})

        result = server.pco_person_song_history(
            account="unw",
            person_name="Alex Morgan",
            limit=5,
            include_declined=True,
        )

        self.assertEqual(result["account"], "unw")
        self.assertEqual(result["org_name"], "Main Campus")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["returned_plans"], 1)

        plan = result["plans"][0]
        self.assertEqual(plan["account"], "unw")
        self.assertEqual(plan["org_name"], "Main Campus")
        assignment = plan["assignments"][0]
        self.assertEqual(assignment["status"], "D")
        self.assertEqual(assignment["decline_reason"], "Sick")
        self.assertEqual(assignment["status_updated_at"], "2026-11-15T12:00:00Z")
        self.assertEqual(plan["songs"][0]["title"], "Great Are You Lord")
        self.assertEqual(plan["songs"][0]["key"], "G")


if __name__ == "__main__":
    unittest.main()

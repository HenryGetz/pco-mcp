"""pco_person_upcoming tests: upcoming-plan window, declines, candidates, fan-out.

No network access. Canned PCO payloads are served through the same FakeClient /
ServerTestBase helpers the rest of the server suite uses. Plan dates are built
relative to ``date.today()`` so the window bound tests stay valid on any day.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

import pco_accounts as pa
import server

try:  # repo-root runs (tests is a package)
    from .test_server_tools import (
        CREDS,
        PEOPLE_PAYLOAD,
        FakeClient,
        FakeResponse,
        ServerTestBase,
        _path_only,
        ok,
    )
except ImportError:  # discovery with tests/ itself on sys.path, or direct run
    from test_server_tools import (  # type: ignore[no-redef]
        CREDS,
        PEOPLE_PAYLOAD,
        FakeClient,
        FakeResponse,
        ServerTestBase,
        _path_only,
        ok,
    )


def _service_type(st_id: str, name: str) -> dict:
    return {
        "id": st_id,
        "type": "ServiceType",
        "attributes": {"name": name, "frequency": "Weekly"},
    }


def _person(person_id: str, name: str, archived: bool = False) -> dict:
    return {
        "id": str(person_id),
        "type": "Person",
        "attributes": {"full_name": name, "archived": archived},
    }


def _plan(
    plan_id: str,
    service_type_id: str,
    title: str,
    sort_date: str,
    dates: str | None = None,
) -> dict:
    return {
        "id": plan_id,
        "service_type_id": service_type_id,
        "title": title,
        "sort_date": sort_date,
        "dates": sort_date if dates is None else dates,
    }


def _plan_time(
    time_id: str,
    time_type: str,
    starts_at: str,
    ends_at: str,
    name: str = "Service",
) -> dict:
    return {
        "id": time_id,
        "type": "PlanTime",
        "attributes": {
            "name": name,
            "time_type": time_type,
            "starts_at": starts_at,
            "ends_at": ends_at,
        },
    }


def _assignment(
    plan_id: str,
    *,
    status: str = "C",
    person_id: str = "9",
    name: str = "Alex Morgan",
    position: str = "Acoustic Guitar",
    decline_reason: str | None = None,
) -> dict:
    return {
        "id": f"tm-{plan_id}",
        "type": "TeamMember",
        "attributes": {
            "status": status,
            "decline_reason": decline_reason,
            "status_updated_at": "2026-11-20T12:00:00Z",
            "team_position_name": position,
            "name": name,
        },
        "relationships": {"person": {"data": {"id": person_id, "type": "Person"}}},
    }


def _plan_payload(plan: dict) -> dict:
    return {
        "id": plan["id"],
        "type": "Plan",
        "attributes": {
            "title": plan["title"],
            "dates": plan["dates"],
            "sort_date": plan["sort_date"],
        },
    }


def _default_plan_time(plan: dict) -> dict:
    day = plan["sort_date"][:10]
    return _plan_time(
        f"pt-{plan['id']}",
        "service",
        f"{day}T10:30:00Z",
        f"{day}T12:00:00Z",
    )


def make_handler(
    service_types: list[dict],
    plans: list[dict],
    *,
    people_payload: dict | None = None,
    assignments: dict[tuple[str, str], list[dict]] | None = None,
    plan_times: dict[tuple[str, str], list[dict]] | None = None,
    plan_list_errors: set[str] | None = None,
    team_member_errors: set[tuple[str, str]] | None = None,
    plan_times_errors: set[tuple[str, str]] | None = None,
):
    """Canned pco_person_upcoming responses; query params (filters/order) ignored.

    Plans are returned for their service type in declaration order, so the tool
    under test must apply the date window and max_plans itself. Every plan
    defaults to one confirmed Alex Morgan (person 9) assignment.

    ``plan_list_errors`` (service type ids), ``team_member_errors`` and
    ``plan_times_errors`` (service type id, plan id) force a 500 on those GETs.
    """
    sts = {st["id"]: st for st in service_types}
    by_st: dict[str, list[dict]] = {}
    for plan in plans:
        by_st.setdefault(plan["service_type_id"], []).append(plan)
    by_id = {plan["id"]: plan for plan in plans}
    assignments = assignments or {}
    plan_times = plan_times or {}
    plan_list_errors = plan_list_errors or set()
    team_member_errors = team_member_errors or set()
    plan_times_errors = plan_times_errors or set()

    def handler(path, _params):
        p = _path_only(path)
        parts = p.strip("/").split("/")

        if p == "/people":
            return ok(people_payload if people_payload is not None else {"data": []})
        if parts[0] != "service_types":
            return FakeResponse(404, {"errors": [{"title": f"no route {p}"}]})
        if len(parts) == 1:
            return ok({"data": list(service_types)})

        st_id = parts[1]
        if st_id not in sts:
            return FakeResponse(404, {"errors": [{"title": f"no service type {st_id}"}]})
        if len(parts) == 2:
            return ok({"data": sts[st_id]})
        if parts[2] != "plans":
            return FakeResponse(404, {"errors": [{"title": f"no route {p}"}]})
        if len(parts) == 3:
            if st_id in plan_list_errors:
                return FakeResponse(
                    500, {"errors": [{"title": f"plans listing failed for {st_id}"}]}
                )
            return ok({"data": [_plan_payload(item) for item in by_st.get(st_id, [])]})

        plan_id = parts[3]
        plan = by_id.get(plan_id)
        if plan is None:
            return FakeResponse(404, {"errors": [{"title": f"no plan {plan_id}"}]})
        if len(parts) == 4:
            return ok({"data": _plan_payload(plan)})
        if len(parts) != 5:
            return FakeResponse(404, {"errors": [{"title": f"no route {p}"}]})

        if parts[4] == "team_members":
            if (st_id, plan_id) in team_member_errors:
                return FakeResponse(
                    500, {"errors": [{"title": f"team members failed for {plan_id}"}]}
                )
            team = assignments.get((st_id, plan_id))
            return ok({"data": list(team) if team is not None else [_assignment(plan_id)]})
        if parts[4] == "plan_times":
            if (st_id, plan_id) in plan_times_errors:
                return FakeResponse(
                    500, {"errors": [{"title": f"plan times failed for {plan_id}"}]}
                )
            times = plan_times.get((st_id, plan_id))
            return ok({"data": list(times) if times is not None else [_default_plan_time(plan)]})
        return FakeResponse(404, {"errors": [{"title": f"no route {p}"}]})

    return handler


class SingleAccountUpcomingTests(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)

    def _serve(self, handler) -> FakeClient:
        client = FakeClient(handler)
        self.use_clients({"main": client})
        return client

    def test_matches_exact_person_and_returns_windowed_plan_fields(self):
        base = date.today()
        inside = base + timedelta(days=2)
        plan = _plan(
            "11111",
            "st-sun",
            "Sunday Gathering",
            inside.isoformat(),
            inside.strftime("%B %d, %Y"),
        )
        past = _plan(
            "22222",
            "st-youth",
            "Youth Past",
            (base - timedelta(days=3)).isoformat(),
        )
        beyond = _plan(
            "33333",
            "st-sun",
            "Far Future",
            (base + timedelta(days=40)).isoformat(),
        )
        times = [
            _plan_time(
                "pt-1",
                "service",
                f"{inside.isoformat()}T10:30:00Z",
                f"{inside.isoformat()}T12:00:00Z",
            )
        ]
        client = self._serve(make_handler(
            [
                _service_type("st-sun", "Sunday Morning"),
                _service_type("st-youth", "Youth"),
            ],
            [plan, past, beyond],
            people_payload=PEOPLE_PAYLOAD,
            plan_times={("st-sun", "11111"): times},
        ))

        result = server.pco_person_upcoming(
            person_name="Alex Morgan", days=21, account="main"
        )

        self.assertEqual(result["account"], "main")
        self.assertEqual(result["org_name"], "Main Campus")
        self.assertEqual(result["person_name"], "Alex Morgan")
        self.assertEqual(result["person_id"], "9")
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["window"], {
            "from": base.isoformat(),
            "to": (base + timedelta(days=21)).isoformat(),
            "days": 21,
        })
        self.assertEqual(result["matched_plans"], 1)
        self.assertEqual(result["returned_plans"], 1)
        self.assertEqual(result["assignments_count"], 1)
        self.assertTrue(result["scan_complete"])
        self.assertEqual(result["skipped"], [])
        self.assertGreater(result["requests"], 0)
        self.assertEqual(result["retries"], 0)
        self.assertEqual(result["rate_limited"], 0)
        self.assertEqual(result["account_source"], "explicit")

        self.assertEqual([p["plan_id"] for p in result["plans"]], ["11111"])
        got = result["plans"][0]
        self.assertEqual(got["service_type_id"], "st-sun")
        self.assertEqual(got["service_type_name"], "Sunday Morning")
        self.assertEqual(got["title"], "Sunday Gathering")
        self.assertEqual(got["dates"], plan["dates"])
        self.assertEqual(got["sort_date"], inside.isoformat())
        self.assertEqual(got["date"], inside.isoformat())
        self.assertEqual(got["starts_at"], f"{inside.isoformat()}T10:30:00Z")
        self.assertEqual(
            [row["starts_at"] for row in got["plan_times"]],
            [f"{inside.isoformat()}T10:30:00Z"],
        )
        self.assertEqual([row["time_type"] for row in got["plan_times"]], ["service"])
        self.assertIsNone(got.get("plan_times_error"))
        self.assertEqual(got["positions"], ["Acoustic Guitar"])
        self.assertEqual(got["statuses"], ["C"])
        self.assertEqual(len(got["assignments"]), 1)
        self.assertEqual(got["assignments"][0]["status"], "C")
        self.assertEqual(got["account"], "main")
        self.assertEqual(got["org_name"], "Main Campus")

        # FakeClient only defines get(); every recorded call is a relative GET.
        self.assertTrue(client.calls)
        for path, _params in client.calls:
            self.assertIsInstance(path, str)
            self.assertTrue(path.startswith("/"), path)

    def test_declined_only_plan_is_skipped_unless_requested(self):
        base = date.today()
        confirmed = _plan(
            "11111", "st-sun", "Sunday Gathering", (base + timedelta(days=1)).isoformat()
        )
        declined = _plan(
            "22222", "st-sun", "Midweek", (base + timedelta(days=2)).isoformat()
        )
        assignments = {
            ("st-sun", "11111"): [_assignment("11111")],
            ("st-sun", "22222"): [
                _assignment(
                    "22222",
                    status="D",
                    position="Drums",
                    decline_reason="Sick",
                )
            ],
        }
        self._serve(make_handler(
            [_service_type("st-sun", "Sunday Morning")],
            [confirmed, declined],
            people_payload=PEOPLE_PAYLOAD,
            assignments=assignments,
        ))

        default = server.pco_person_upcoming(person_name="Alex Morgan", account="main")

        self.assertEqual([p["plan_id"] for p in default["plans"]], ["11111"])
        self.assertEqual(default["matched_plans"], 1)
        self.assertEqual(default["returned_plans"], 1)
        self.assertEqual(default["assignments_count"], 1)
        self.assertEqual(default["plans"][0]["statuses"], ["C"])

        with_declined = server.pco_person_upcoming(
            person_name="Alex Morgan", account="main", include_declined=True
        )

        self.assertEqual(
            [p["plan_id"] for p in with_declined["plans"]], ["11111", "22222"]
        )
        self.assertEqual(with_declined["matched_plans"], 2)
        self.assertEqual(with_declined["assignments_count"], 2)
        declined_plan = with_declined["plans"][1]
        self.assertEqual(declined_plan["statuses"], ["D"])
        self.assertEqual(len(declined_plan["assignments"]), 1)
        self.assertEqual(declined_plan["assignments"][0]["status"], "D")
        self.assertEqual(declined_plan["assignments"][0]["position"], "Drums")

    def test_ambiguous_exact_people_returns_candidates_without_scanning(self):
        base = date.today()
        plan = _plan("11111", "st-sun", "Sunday Gathering", base.isoformat())
        client = self._serve(make_handler(
            [_service_type("st-sun", "Sunday Morning")],
            [plan],
            people_payload={
                "data": [_person("9", "Alex Morgan"), _person("10", "Alex Morgan")]
            },
        ))

        result = server.pco_person_upcoming(person_name="Alex Morgan", account="main")

        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["plans"], [])
        self.assertEqual(result["matched_plans"], 0)
        self.assertEqual(result["returned_plans"], 0)
        self.assertEqual(result["assignments_count"], 0)
        self.assertEqual({row["id"] for row in result["candidates"]}, {"9", "10"})
        self.assertFalse(
            any("team_members" in path for path, _params in client.calls),
            client.calls,
        )

    def test_no_exact_person_returns_candidates_without_scanning(self):
        base = date.today()
        plan = _plan("11111", "st-sun", "Sunday Gathering", base.isoformat())
        client = self._serve(make_handler(
            [_service_type("st-sun", "Sunday Morning")],
            [plan],
            people_payload={"data": [_person("11", "Taylor Reed")]},
        ))

        result = server.pco_person_upcoming(person_name="Alex Morgan", account="main")

        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["plans"], [])
        self.assertEqual(result["matched_plans"], 0)
        self.assertTrue(result["candidates"])
        self.assertIn("Taylor Reed", {row["name"] for row in result["candidates"]})
        self.assertFalse(
            any("team_members" in path for path, _params in client.calls),
            client.calls,
        )

    def test_window_bounds_are_today_and_today_plus_days(self):
        base = date.today()
        days = 10
        plans = [
            _plan(
                "90001",
                "st-sun",
                "Yesterday",
                (base - timedelta(days=1)).isoformat(),
            ),
            _plan("90002", "st-sun", "Today", base.isoformat()),
            _plan(
                "90003",
                "st-sun",
                "Last Day",
                (base + timedelta(days=days)).isoformat(),
            ),
            _plan(
                "90004",
                "st-sun",
                "One Day Too Far",
                (base + timedelta(days=days + 1)).isoformat(),
            ),
        ]
        self._serve(make_handler(
            [_service_type("st-sun", "Sunday Morning")],
            plans,
            people_payload=PEOPLE_PAYLOAD,
        ))

        result = server.pco_person_upcoming(
            person_name="Alex Morgan", days=days, account="main"
        )

        self.assertEqual(
            [p["plan_id"] for p in result["plans"]], ["90002", "90003"]
        )
        self.assertEqual(result["window"], {
            "from": base.isoformat(),
            "to": (base + timedelta(days=days)).isoformat(),
            "days": days,
        })
        self.assertEqual(result["matched_plans"], 2)
        self.assertEqual(result["returned_plans"], 2)

    def test_max_plans_truncation_marks_scan_incomplete(self):
        base = date.today()
        plans = [
            _plan("91001", "st-sun", "One", base.isoformat()),
            _plan("91002", "st-sun", "Two", (base + timedelta(days=1)).isoformat()),
            _plan("91003", "st-sun", "Three", (base + timedelta(days=2)).isoformat()),
        ]
        self._serve(make_handler(
            [_service_type("st-sun", "Sunday Morning")],
            plans,
            people_payload=PEOPLE_PAYLOAD,
        ))

        result = server.pco_person_upcoming(
            person_name="Alex Morgan", days=21, max_plans=2, account="main"
        )

        self.assertEqual(len(result["plans"]), 2)
        self.assertEqual(result["returned_plans"], 2)
        self.assertEqual(result["matched_plans"], 2)
        self.assertFalse(result["scan_complete"])

    def test_service_type_plan_listing_failure_marks_scan_incomplete(self):
        base = date.today()
        healthy = _plan(
            "92001",
            "st-sun",
            "Sunday Gathering",
            (base + timedelta(days=1)).isoformat(),
        )
        self._serve(make_handler(
            [
                _service_type("st-sun", "Sunday Morning"),
                _service_type("st-youth", "Youth"),
            ],
            [healthy],
            people_payload=PEOPLE_PAYLOAD,
            plan_list_errors={"st-youth"},
        ))

        # The 500 is retried with backoff; stub the sleeps so the test stays fast.
        with mock.patch.object(server.time, "sleep", return_value=None):
            result = server.pco_person_upcoming(
                person_name="Alex Morgan", days=21, account="main"
            )

        self.assertEqual([p["plan_id"] for p in result["plans"]], ["92001"])
        self.assertEqual(result["scanned_plans"], 1)
        self.assertTrue(
            any("st-youth" in row for row in result["skipped"]), result["skipped"]
        )
        self.assertFalse(result["scan_complete"])

    def test_team_member_failure_marks_scan_incomplete(self):
        base = date.today()
        good = _plan(
            "92002",
            "st-sun",
            "Sunday Gathering",
            (base + timedelta(days=1)).isoformat(),
        )
        broken = _plan(
            "92003", "st-sun", "Midweek", (base + timedelta(days=2)).isoformat()
        )
        self._serve(make_handler(
            [_service_type("st-sun", "Sunday Morning")],
            [good, broken],
            people_payload=PEOPLE_PAYLOAD,
            team_member_errors={("st-sun", "92003")},
        ))

        with mock.patch.object(server.time, "sleep", return_value=None):
            result = server.pco_person_upcoming(
                person_name="Alex Morgan", days=21, account="main"
            )

        self.assertEqual([p["plan_id"] for p in result["plans"]], ["92002"])
        self.assertEqual(result["scanned_plans"], 2)
        self.assertTrue(
            any("92003" in row for row in result["skipped"]), result["skipped"]
        )
        self.assertFalse(result["scan_complete"])

    def test_plan_times_failure_does_not_clear_scan_complete(self):
        base = date.today()
        plan = _plan(
            "92004",
            "st-sun",
            "Sunday Gathering",
            (base + timedelta(days=1)).isoformat(),
        )
        self._serve(make_handler(
            [_service_type("st-sun", "Sunday Morning")],
            [plan],
            people_payload=PEOPLE_PAYLOAD,
            plan_times_errors={("st-sun", "92004")},
        ))

        with mock.patch.object(server.time, "sleep", return_value=None):
            result = server.pco_person_upcoming(
                person_name="Alex Morgan", days=21, account="main"
            )

        self.assertEqual([p["plan_id"] for p in result["plans"]], ["92004"])
        self.assertEqual(result["plans"][0]["plan_times"], [])
        self.assertTrue(result["plans"][0]["plan_times_error"])
        self.assertEqual(result["skipped"], [])
        self.assertTrue(result["scan_complete"])


class FanOutUpcomingTests(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)

    def test_all_accounts_merges_and_sorts_plans(self):
        base = date.today()
        main_plans = [
            _plan("10001", "st-main", "Main Today", base.isoformat()),
            _plan(
                "10002",
                "st-main",
                "Main Later",
                (base + timedelta(days=2)).isoformat(),
            ),
        ]
        north_plans = [
            _plan(
                "20001",
                "st-north",
                "North Sunday",
                (base + timedelta(days=1)).isoformat(),
            )
        ]
        main_client = FakeClient(make_handler(
            [_service_type("st-main", "Main Sunday")],
            main_plans,
            people_payload=PEOPLE_PAYLOAD,
        ))
        north_client = FakeClient(make_handler(
            [_service_type("st-north", "North Sunday")],
            north_plans,
            people_payload=PEOPLE_PAYLOAD,
        ))
        self.use_clients({"main": main_client, "north": north_client})

        envelope = server.pco_person_upcoming(
            person_name="Alex Morgan", days=21, account="all"
        )

        self.assertEqual(envelope["account"], "all")
        self.assertEqual(envelope["account_source"], "all")
        self.assertEqual(envelope["errors"], [])
        self.assertEqual(
            {row["account"] for row in envelope["results"]}, {"main", "north"}
        )
        self.assertEqual(
            {row["account"]: row["org_name"] for row in envelope["results"]},
            {"main": "Main Campus", "north": "North Campus"},
        )

        plans = envelope["plans"]
        self.assertEqual(
            [p["plan_id"] for p in plans], ["10001", "20001", "10002"]
        )
        self.assertEqual([p["date"] for p in plans], sorted(p["date"] for p in plans))
        self.assertEqual(envelope["count"], 3)

        expected = {
            "10001": ("main", "Main Campus", "Main Sunday"),
            "10002": ("main", "Main Campus", "Main Sunday"),
            "20001": ("north", "North Campus", "North Sunday"),
        }
        for plan in plans:
            account, org, st_name = expected[plan["plan_id"]]
            self.assertEqual(plan["account"], account)
            self.assertEqual(plan["org_name"], org)
            self.assertEqual(plan["service_type_name"], st_name)

        summary = envelope["summary"]
        self.assertEqual(summary["accounts_queried"], 2)
        self.assertEqual(summary["accounts_succeeded"], 2)
        self.assertEqual(summary["accounts_failed"], 0)
        self.assertEqual(summary["matched_plans"], 3)
        self.assertEqual(summary["assignments"], 3)

    def test_partial_failure_keeps_successes_and_records_error(self):
        base = date.today()
        main_plans = [
            _plan(
                "10001",
                "st-main",
                "Main Sunday",
                (base + timedelta(days=1)).isoformat(),
            )
        ]
        self.use_clients({
            "main": FakeClient(make_handler(
                [_service_type("st-main", "Main Sunday")],
                main_plans,
                people_payload=PEOPLE_PAYLOAD,
            )),
            "north": pa.AccountError("401 unauthorized for north"),
        })

        envelope = server.pco_person_upcoming(
            person_name="Alex Morgan", days=21, account="all"
        )

        self.assertEqual(envelope["account"], "all")
        self.assertEqual(envelope["account_source"], "all")
        self.assertEqual([row["account"] for row in envelope["results"]], ["main"])
        self.assertEqual([p["plan_id"] for p in envelope["plans"]], ["10001"])
        self.assertEqual(envelope["plans"][0]["account"], "main")
        self.assertEqual(envelope["count"], 1)

        self.assertEqual(len(envelope["errors"]), 1)
        error = envelope["errors"][0]
        self.assertEqual(error["account"], "north")
        self.assertEqual(error["org_name"], "North Campus")
        self.assertIn("401", str(error["error"]))

        summary = envelope["summary"]
        self.assertEqual(summary["accounts_queried"], 2)
        self.assertEqual(summary["accounts_succeeded"], 1)
        self.assertEqual(summary["accounts_failed"], 1)
        self.assertEqual(summary["matched_plans"], 1)
        self.assertEqual(summary["assignments"], 1)


class ValidationUpcomingTests(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_creds(CREDS)
        self.client = FakeClient(make_handler(
            [_service_type("st-sun", "Sunday Morning")],
            [_plan("11111", "st-sun", "Sunday Gathering", date.today().isoformat())],
            people_payload=PEOPLE_PAYLOAD,
        ))
        self.use_clients({"main": self.client})

    def test_invalid_arguments_raise_value_error_before_any_request(self):
        cases = [
            {"person_name": ""},
            {"person_name": "   "},
            {"person_name": "Alex Morgan", "days": 0},
            {"person_name": "Alex Morgan", "days": -3},
            {"person_name": "Alex Morgan", "max_plans": 0},
            {"person_name": "Alex Morgan", "max_plans": -1},
        ]
        for kwargs in cases:
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    server.pco_person_upcoming(account="main", **kwargs)

        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()

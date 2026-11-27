import contextlib
import copy
import hashlib
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import time
import unittest

import rehearsal_lease as lease


RUN = "a" * 32
PREFIX = "terrace-r-" + RUN


def resource(rid, name, owned=True, **fields):
    return {"id": rid, "name": name, "labels": {lease.LABEL: RUN} if owned else {}, **fields}


def inventory():
    result = {kind: [] for kind in lease.INVENTORY}
    result["servers"] = [resource(1, "existing", False, status="running")]
    result["primary_ips"] = [resource(2, "existing-ip", False, assignee_id=1)]
    result["ssh_keys"] = [resource(3, "existing-key", False)]
    return result


def write_reviewed_input(root, config, roles=None):
    """Write a reviewed, dispatched phase input. Without roles the review has the
    pre-single-host layout, which has no roles key."""
    phase = config["phase"]
    path = root / f"{phase}-input.json"
    review = root / phase / "review.json"
    dispatch = root / f"{phase}-dispatched.json"
    review.parent.mkdir(exist_ok=True)
    for file in (path, review, dispatch):
        file.unlink(missing_ok=True)
    lease.write_private(path, config)
    lease.write_private(review, {"probeOnly": config.get("probeOnly", False),
                                 **({} if roles is None else {"roles": roles}),
                                 "inputSha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    lease.write_private(dispatch, {"run": config["run"], "reviewSha256": hashlib.sha256(review.read_bytes()).hexdigest()})
    return path


def populate(data, phase, server_id, ip_id):
    name = PREFIX + "-" + phase + "-app"
    data["servers"].append(resource(server_id, name, volumes=[], private_net=[], load_balancers=[],
                                     public_net={"ipv4": {"id": ip_id}, "ipv6": None,
                                                 "firewalls": [{"id": 8}], "floating_ips": []}))
    data["primary_ips"].append(resource(ip_id, name + "-ip", assignee_id=server_id))


class FakeApi:
    def __init__(self, data):
        self.data = data
        self.deleted = []

    def inventory(self):
        return copy.deepcopy(self.data)

    def get(self, kind, rid):
        return next((copy.deepcopy(row) for row in self.data[kind] if row["id"] == rid), None)

    def delete(self, kind, rid):
        self.deleted.append((kind, rid))
        self.data[kind] = [row for row in self.data[kind] if row["id"] != rid]
        if kind == "servers":
            for row in self.data["primary_ips"]:
                if row.get("assignee_id") == rid:
                    row["assignee_id"] = None


class RehearsalLeaseTest(unittest.TestCase):
    def setUp(self):
        self.data = inventory()
        self.manifest = {"run": RUN, "baseline": lease.baseline_from(self.data)}
        populate(self.data, "original", 4, 5)
        self.data["firewalls"] = [resource(8, PREFIX + "-firewall", applied_to=[])]
        self.data["ssh_keys"].append(resource(9, PREFIX + "-key"))
        self.api = FakeApi(self.data)

    def clean(self, phase="final", known=None):
        return lease.clean(self.api, self.manifest, phase, known or {}, deadline=time.monotonic() + 2, delay=0)

    def test_retire_original_then_delete_replacement_and_shared_foundation(self):
        first = self.clean("original", {"servers": [4], "primary_ips": [5]})
        self.assertEqual(self.api.deleted, [("servers", 4), ("primary_ips", 5)])
        self.assertEqual(first["absenceReads"], 3)
        populate(self.data, "replacement", 6, 7)
        final = self.clean(known={"servers": [4, 6], "primary_ips": [5, 7]})
        self.assertEqual(self.api.deleted[2:], [("servers", 6), ("primary_ips", 7),
                                               ("firewalls", 8), ("ssh_keys", 9)])
        self.assertEqual(final["verifiedAbsent"]["servers"], [4, 6])
        self.assertEqual(lease.baseline_from(self.data), self.manifest["baseline"])

    def test_never_deletes_baseline_label_collision_or_foreign_attachment(self):
        self.data["servers"][0].update(name=PREFIX + "-original-relay", labels={lease.LABEL: RUN})
        self.data["servers"][1]["public_net"]["ipv4"]["id"] = 2
        with self.assertRaises(lease.Rejected):
            self.clean()
        self.assertEqual(self.api.deleted, [])

    def test_unknown_type_and_renamed_recorded_id_prevent_success(self):
        self.data["servers"][1].update(name="changed", labels={})
        self.data["primary_ips"][1].update(name="changed-ip", labels={})
        self.data["volumes"] = [resource(10, PREFIX + "-volume")]
        with self.assertRaises(lease.Rejected):
            self.clean(known={"servers": [4], "primary_ips": [5]})
        self.assertNotIn(("servers", 4), self.api.deleted)
        self.assertNotIn(("volumes", 10), self.api.deleted)

    def test_baseline_change_detected_after_owned_cleanup(self):
        self.data["primary_ips"][0]["assignee_id"] = None
        with self.assertRaises(lease.Rejected):
            self.clean()
        self.assertEqual(self.api.deleted, [("servers", 4), ("primary_ips", 5),
                                           ("firewalls", 8), ("ssh_keys", 9)])

    def test_partial_create_without_attached_ip_and_unknown_extra_inventory(self):
        self.data["servers"][1]["public_net"]["ipv4"] = None
        self.data["primary_ips"][1]["assignee_id"] = None
        self.data["volumes"] = [resource(10, "unidentified", False)]
        with self.assertRaises(lease.Rejected):
            self.clean()
        self.assertEqual(self.api.deleted, [("servers", 4), ("primary_ips", 5),
                                           ("firewalls", 8), ("ssh_keys", 9)])

    def test_original_retirement_rejects_early_replacement(self):
        populate(self.data, "replacement", 6, 7)
        with self.assertRaises(lease.Rejected):
            self.clean("original")
        self.assertEqual(self.api.deleted, [])

    def test_action_and_ambiguous_connection_are_settled_before_dependencies(self):
        delete = self.api.delete
        events = []

        def delayed_delete(kind, rid):
            if (kind, rid) == ("servers", 4):
                events.append("server-delete")
                if events.count("server-delete") == 1:
                    raise lease.Retryable
                delete(kind, rid)
                return 42
            self.assertIn("action-success", events)
            return delete(kind, rid)

        def action_status(rid):
            self.assertEqual(rid, 42)
            if "action-running" not in events:
                events.append("action-running")
                return "running"
            events.append("action-success")
            return "success"

        self.api.delete = delayed_delete
        self.api.action_status = action_status
        recorded = []
        receipt = lease.clean(self.api, self.manifest, "final", {}, deadline=time.monotonic() + 2,
                              delay=0, record_action=recorded.append)
        self.assertEqual(recorded, [42])
        self.assertEqual(receipt["actions"], {42: "success"})

    def test_private_exclusive_file_and_strict_reader(self):
        with tempfile.TemporaryDirectory() as name:
            directory = lease.private_directory(Path(name))
            path = directory / "lease.json"
            lease.write_private(path, {"schema": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn(b'"schema": 1', lease.read_private(path, 1024))
            with self.assertRaises(FileExistsError):
                lease.write_private(path, {"schema": 2})
            symlink = directory / "symlink"
            symlink.symlink_to(path)
            with self.assertRaises(OSError):
                lease.read_private(symlink, 1024)
            os.chmod(path, 0o644)
            with self.assertRaises(lease.Rejected):
                lease.read_private(path, 1024)


class SingleHostCohortTest(unittest.TestCase):
    """The original-only `"roles": ["app"]` cohort: one installable app host."""

    CONFIG = {"run": RUN, "phase": "original", "roles": ["app"]}

    def setUp(self):
        self.data = inventory()
        # One pre-existing row of every other kind, so absence must preserve each.
        for rid, kind in enumerate(lease.INVENTORY[len(lease.DELETION_ORDER):], start=100):
            self.data[kind] = [resource(rid, "existing-" + kind, False)]
        self.manifest = {"run": RUN, "baseline": lease.baseline_from(self.data)}
        populate(self.data, "original", 4, 5)
        self.data["firewalls"] = [resource(8, PREFIX + "-firewall", applied_to=[])]
        self.data["ssh_keys"].append(resource(9, PREFIX + "-key"))
        self.api = FakeApi(self.data)

    def clean(self, phase="final", known=None):
        return lease.clean(self.api, self.manifest, phase, known or {}, deadline=time.monotonic() + 2, delay=0)

    def test_shape_is_exact_and_original_only(self):
        self.assertEqual(lease.cohort_roles(self.CONFIG, "original"), ("app",))
        self.assertEqual(lease.cohort_roles({"run": RUN, "phase": "original"}, "original"), ("app", "relay"))
        self.assertEqual(lease.cohort_roles({"probeOnly": True}, "original"), ("relay",))
        for roles in (["relay"], ["app", "relay"], ["app", "app"], [], "app", ("app",), None, [["app"]]):
            with self.assertRaises(lease.Rejected):
                lease.cohort_roles({**self.CONFIG, "roles": roles}, "original")
        for phase in ("replacement", "foundation", "final"):
            with self.assertRaises(lease.Rejected):
                lease.cohort_roles(self.CONFIG, phase)
        with self.assertRaises(lease.Rejected):
            lease.cohort_roles({**self.CONFIG, "probeOnly": True}, "original")

    def test_review_binds_the_roles(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            write_reviewed_input(root, self.CONFIG, roles=["app"])
            self.assertEqual(lease.reviewed_roles(root, "original"), ("app",))
            for roles in (None, ["app", "relay"], ["relay"], []):
                write_reviewed_input(root, self.CONFIG, roles=roles)
                with self.assertRaisesRegex(lease.Rejected, "no longer matches"):
                    lease.reviewed_phase_input(root, "original")
            write_reviewed_input(root, {"run": RUN, "phase": "original"}, roles=["app"])
            with self.assertRaisesRegex(lease.Rejected, "no longer matches"):
                lease.reviewed_phase_input(root, "original")
            # The two existing shapes still read without a recorded roles key.
            write_reviewed_input(root, {"run": RUN, "phase": "original"})
            self.assertEqual(lease.reviewed_roles(root, "original"), ("app", "relay"))
            write_reviewed_input(root, {"run": RUN, "phase": "original", "probeOnly": True})
            self.assertEqual(lease.reviewed_roles(root, "original"), ("relay",))

    def test_cleanup_deletes_only_the_named_and_labelled_run_resources(self):
        receipt = self.clean(known={"servers": [4], "primary_ips": [5]})
        self.assertEqual(self.api.deleted, [("servers", 4), ("primary_ips", 5), ("firewalls", 8), ("ssh_keys", 9)])
        self.assertEqual(receipt["verifiedAbsent"], {"servers": [4], "primary_ips": [5],
                                                     "firewalls": [8], "ssh_keys": [9]})
        self.assertEqual((receipt["absenceReads"], receipt["baselinePreserved"]), (3, True))
        self.assertEqual(lease.baseline_from(self.data), self.manifest["baseline"])

    def test_ownership_needs_name_and_label_and_never_a_baseline_id(self):
        for change in ({"labels": {}}, {"name": PREFIX + "-original-other"},
                       {"labels": {lease.LABEL: "b" * 32}}):
            data = copy.deepcopy(self.data)
            data["servers"][1].update(change)
            owned, collisions = lease.select_owned(RUN, self.manifest["baseline"], data, "final")
            self.assertTrue(collisions)
            self.assertNotIn(4, [row["id"] for row in owned["servers"]])
            api = FakeApi(data)
            with self.assertRaises(lease.Rejected):
                lease.clean(api, self.manifest, "final", {"servers": [4]}, deadline=time.monotonic() + 2, delay=0)
            self.assertNotIn(("servers", 4), api.deleted)
        # Baseline rows given the run's exact name and label stay out of scope.
        for kind, name in (("servers", PREFIX + "-original-app"), ("primary_ips", PREFIX + "-original-app-ip"),
                           ("ssh_keys", PREFIX + "-key")):
            data = copy.deepcopy(self.data)
            data[kind][0].update(name=name, labels={lease.LABEL: RUN})
            _, collisions = lease.select_owned(RUN, self.manifest["baseline"], data, "final")
            self.assertTrue(collisions)
            api = FakeApi(data)
            with self.assertRaises(lease.Rejected):
                lease.clean(api, self.manifest, "final", {}, deadline=time.monotonic() + 2, delay=0)
            self.assertNotIn((kind, data[kind][0]["id"]), api.deleted)
            self.assertFalse(any(rid in (1, 2, 3) for _, rid in api.deleted))

    def test_absence_compares_every_inventory_kind_with_the_baseline(self):
        for kind in lease.INVENTORY:
            for extra in (resource(200, "unrecognized", False, assignee_id=None),
                          resource(200, PREFIX + "-" + kind, assignee_id=None)):
                data = copy.deepcopy(self.data)
                data[kind].append(extra)
                api = FakeApi(data)
                with self.assertRaises(lease.Rejected, msg=kind):
                    lease.clean(api, self.manifest, "final", {}, deadline=time.monotonic() + 2, delay=0)
                self.assertNotIn((kind, 200), api.deleted)

    def test_retire_original_refuses_the_single_host_cohort(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            lease.write_private(root / "original-created.json", {"servers": [4], "primary_ips": [5]})
            lease.write_private(root / "backup-verified.json", {"run": RUN, "verified": True, "servers": [4]})
            write_reviewed_input(root, self.CONFIG, roles=["app"])
            with self.assertRaisesRegex(lease.Rejected, "requires the reviewed two-role cohort"):
                lease.require_retirement_scope(root, self.manifest)
            write_reviewed_input(root, {"run": RUN, "phase": "original", "probeOnly": True})
            with self.assertRaisesRegex(lease.Rejected, "requires the reviewed two-role cohort"):
                lease.require_retirement_scope(root, self.manifest)
            # The two-role cohort keeps its existing backup-receipt rule.
            write_reviewed_input(root, {"run": RUN, "phase": "original"})
            with self.assertRaisesRegex(lease.Rejected, "off-host backup receipt"):
                lease.require_retirement_scope(root, self.manifest)
            for path in ("original-created.json", "backup-verified.json"):
                (root / path).unlink()
            lease.write_private(root / "original-created.json", {"servers": [4, 6], "primary_ips": [5, 7]})
            lease.write_private(root / "backup-verified.json", {"run": RUN, "verified": True, "servers": [6, 4]})
            lease.require_retirement_scope(root, self.manifest)


def page(endpoint, rows, number, last):
    return {endpoint: rows, "meta": {"pagination": {"page": number, "next_page": None if last else number + 1}}}


def datacenter(name, location, available):
    return {"id": len(name), "name": name, "location": {"name": location},
            "server_types": {"supported": [1, 2, 3], "available": available, "available_for_migration": []}}


class RecordingOpener:
    """Stands in for urllib: answers GET listings from fixed pages and records every request's method and route."""

    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def open(self, request, timeout):
        route = request.full_url.removeprefix("https://api.hetzner.cloud/v1/")
        self.requests.append((request.get_method(), route, request.data))
        endpoint, _, query = route.partition("?")
        number = int(dict(part.split("=") for part in query.split("&"))["page"])
        body = json.dumps(self.pages[endpoint][number - 1]).encode()
        return io.BytesIO(body)


class AvailabilityTest(unittest.TestCase):
    TYPES = [page("server_types", [{"id": 47, "name": "cpx42"}], 1, True)]

    def api(self, datacenters, types=None):
        api = lease.Api("t" * 32)
        api.opener = RecordingOpener({"server_types": types or self.TYPES, "datacenters": datacenters})
        return api

    def test_available_in_the_location(self):
        api = self.api([page("datacenters", [datacenter("fsn1-dc14", "fsn1", [47]),
                                             datacenter("nbg1-dc3", "nbg1", [12, 47])], 1, True)])
        found = lease.availability(api, "cpx42", "nbg1")
        self.assertEqual(found, {"serverType": "cpx42", "location": "nbg1", "serverTypeId": 47,
                                 "availableIn": ["nbg1-dc3"]})

    def test_unavailable_or_only_supported(self):
        api = self.api([page("datacenters", [datacenter("nbg1-dc3", "nbg1", [12])], 1, True)])
        with self.assertRaisesRegex(lease.Rejected, "unavailable in location"):
            lease.availability(api, "cpx42", "nbg1")

    def test_available_only_in_another_location(self):
        api = self.api([page("datacenters", [datacenter("fsn1-dc14", "fsn1", [47]),
                                             datacenter("nbg1-dc3", "nbg1", [])], 1, True)])
        with self.assertRaisesRegex(lease.Rejected, "unavailable in location"):
            lease.availability(api, "cpx42", "nbg1")
        api = self.api([page("datacenters", [datacenter("fsn1-dc14", "fsn1", [47])], 1, True)])
        with self.assertRaisesRegex(lease.Rejected, "no datacenter"):
            lease.availability(api, "cpx42", "nbg1")

    def test_unknown_or_ambiguous_type(self):
        dcs = [page("datacenters", [datacenter("nbg1-dc3", "nbg1", [47])], 1, True)]
        for rows in ([], [{"id": 47, "name": "cpx42"}, {"id": 48, "name": "cpx42"}], [{"id": 9, "name": "cpx22"}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(lease.Rejected, "unknown"):
                lease.availability(self.api(dcs, [page("server_types", rows, 1, True)]), "cpx42", "nbg1")

    def test_malformed_responses_refused(self):
        bad = (
            [page("datacenters", [{"name": "nbg1-dc3", "location": {"name": "nbg1"}}], 1, True)],
            [page("datacenters", [datacenter("nbg1-dc3", "nbg1", ["47"])], 1, True)],
            [page("datacenters", [datacenter("nbg1-dc3", "nbg1", None)], 1, True)],
            [{"datacenters": {}, "meta": {"pagination": {"next_page": None}}}],
            [{"datacenters": []}],
            [page("datacenters", [], 1, False) | {"meta": {"pagination": {"next_page": 7}}}],
        )
        for pages in bad:
            with self.subTest(pages=pages), self.assertRaises((lease.Rejected, KeyError, TypeError)):
                lease.availability(self.api(pages), "cpx42", "nbg1")
        with self.assertRaises(lease.Rejected):
            lease.availability(self.api([]), "cpx42; x", "nbg1")
        with self.assertRaises((lease.Rejected, KeyError, TypeError)):
            lease.availability(self.api([], [page("server_types", [{"id": 0, "name": "cpx42"}], 1, True)]),
                               "cpx42", "nbg1")

    def test_pagination_is_followed(self):
        api = self.api([page("datacenters", [datacenter("fsn1-dc14", "fsn1", [47])], 1, False),
                        page("datacenters", [datacenter("hel1-dc2", "hel1", [47])], 2, False),
                        page("datacenters", [datacenter("nbg1-dc3", "nbg1", [47])], 3, True)])
        self.assertEqual(lease.availability(api, "cpx42", "nbg1")["availableIn"], ["nbg1-dc3"])
        routes = [route for _, route, _ in api.opener.requests]
        self.assertEqual([route for route in routes if route.startswith("datacenters")],
                         [f"datacenters?per_page=50&page={n}" for n in (1, 2, 3)])
        self.assertEqual(routes[0], "server_types?name=cpx42&per_page=50&page=1")

    def test_issues_get_requests_only(self):
        api = self.api([page("datacenters", [datacenter("nbg1-dc3", "nbg1", [47])], 1, True)])
        lease.availability(api, "cpx42", "nbg1")
        api2 = self.api([page("datacenters", [datacenter("nbg1-dc3", "nbg1", [])], 1, True)])
        with self.assertRaises(lease.Rejected):
            lease.availability(api2, "cpx42", "nbg1")
        requests = api.opener.requests + api2.opener.requests
        self.assertTrue(requests)
        self.assertEqual({(method, data) for method, _, data in requests}, {("GET", None)})

    def test_main_availability_writes_nothing(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "run"
            root.mkdir(mode=0o700)
            token = Path(name) / "token"
            token.write_text("t" * 32)
            token.chmod(0o600)
            calls = []
            original = lease.availability
            lease.availability = lambda api, kind, where: calls.append((kind, where)) or {
                "serverType": kind, "location": where, "serverTypeId": 1, "availableIn": ["nbg1-dc3"]}
            argv = sys.argv
            sys.argv = ["rehearsal_lease.py", "availability", "--directory", str(root), "--credential-file",
                        str(token), "--server-type", "cpx42", "--location", "nbg1"]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(lease.main(), 0)
                lease.availability = lambda *_: (_ for _ in ()).throw(lease.Rejected("server type unavailable in location"))
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(lease.main(), 1)
                self.assertIn("unavailable in location", err.getvalue())
            finally:
                lease.availability = original
                sys.argv = argv
            self.assertEqual(calls, [("cpx42", "nbg1")])
            self.assertEqual(list(root.iterdir()), [], "availability wrote into the run directory")


if __name__ == "__main__":
    unittest.main()

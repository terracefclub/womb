import copy
import os
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


if __name__ == "__main__":
    unittest.main()

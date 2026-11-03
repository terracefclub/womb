import importlib.util
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import gspread
import requests
from google.auth.credentials import AnonymousCredentials

source = Path(__file__).resolve().parents[2] / "windmill/f/Membership/record_completed_membership.py"
spec = importlib.util.spec_from_file_location("roster", source)
roster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roster)

MEMBER = {"name": "=Spoof()", "email": "sample@example.invalid", "membership": "standard", "installments": 2}


class SyntheticSheets(requests.Session):
    """Exercise gspread over an in-memory Sheets transport. No socket or credential is used."""

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.markers = {}
        self.rows = []
        self.headers = ["unrelated", "email", "installments", "name", "membership"]
        self.lose_response = False
        self.before_commit_outage = False
        self.lookup_outage = False
        self.read_outage = False
        self.writes = 0
        self.tab = 0
        self.batch_barrier = None

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        assert url.hostname == "sheets.googleapis.com"
        path = unquote(url.path).removeprefix("/v4/spreadsheets/synthetic-roster")
        response = requests.Response()

        def reply(status, payload):
            response.status_code = status
            response._content = json.dumps(payload).encode()
            return response

        if path == "" and request.method == "GET":
            if self.read_outage:
                raise requests.Timeout("synthetic private transport detail")
            return reply(200, {"properties": {"title": "Fixture"}, "sheets": [
                {"properties": {"sheetId": self.tab, "title": "Roster", "index": 0,
                                "gridProperties": {"rowCount": 20, "columnCount": 10}}}
            ]})
        if path.startswith("/values/") and request.method == "GET":
            return reply(200, {"range": "Roster!A1:E1", "majorDimension": "ROWS", "values": [self.headers]})
        if path.startswith("/developerMetadata/") and request.method == "GET":
            if self.lookup_outage:
                raise requests.Timeout("synthetic private transport detail")
            with self.lock:
                marker = self.markers.get(int(path.rsplit("/", 1)[1]))
                if marker:
                    return reply(200, {**marker, "location": {**marker["location"], "locationType": "SHEET"}})
            return reply(404, {"error": {"code": 404, "message": "absent"}})
        if path == ":batchUpdate" and request.method == "POST":
            operations = json.loads(request.body)["requests"]
            if self.batch_barrier:
                self.batch_barrier.wait(timeout=5)
            self.writes += 1
            with self.lock:
                marker_op, row_op = operations
                marker = marker_op["createDeveloperMetadata"]["developerMetadata"]
                row = row_op["appendCells"]
                if marker["metadataId"] in self.markers:
                    return reply(400, {"error": {"code": 400, "message": "duplicate metadata id"}})
                if self.before_commit_outage:
                    return reply(503, {"error": {"code": 503, "message": "synthetic private transport detail"}})
                assert row["sheetId"] == self.tab and row["fields"] == "userEnteredValue"
                self.rows.extend(row["rows"])
                self.markers[marker["metadataId"]] = marker
                if self.lose_response:
                    raise requests.Timeout("synthetic private transport detail")
                return reply(200, {"replies": [{"createDeveloperMetadata": {"developerMetadata": marker}}, {}]})
        raise AssertionError("unexpected Sheets call")


class RosterTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in synthetic tests"))
        network.start()
        self.addCleanup(network.stop)
        self.sheets = SyntheticSheets()
        stub = patch.object(gspread, "service_account_from_dict",
                            side_effect=lambda *args, **kwargs: gspread.Client(AnonymousCredentials(), session=self.sheets))
        self.auth = stub.start()
        self.addCleanup(stub.stop)

    def record(self, document_id=87, member=MEMBER):
        return roster.main(document_id, member, "synthetic-roster", "Roster", {})

    def test_concurrent_deliveries_append_one_row_in_header_order_with_literal_strings(self):
        self.sheets.batch_barrier = threading.Barrier(8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.record(), range(8)))
        self.sheets.batch_barrier = None
        self.assertEqual(results, [{"status": "recorded"}] * 8)
        self.assertEqual(len(self.sheets.markers), 1)
        self.assertEqual(self.sheets.rows, [{"values": [
            {}, {"userEnteredValue": {"stringValue": MEMBER["email"]}},
            {"userEnteredValue": {"numberValue": 2}}, {"userEnteredValue": {"stringValue": "=Spoof()"}},
            {"userEnteredValue": {"stringValue": "standard"}}]}])
        self.record(document_id=88)
        self.assertEqual(len(self.sheets.rows), 2)

    def test_concurrent_conflicting_payloads_cannot_append_both(self):
        self.sheets.batch_barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [pool.submit(self.record, member=member) for member in
                        (MEMBER, {**MEMBER, "membership": "abroad"})]
            self.assertEqual(sum(future.exception() is None for future in outcomes), 1)
            failures = [future.exception() for future in outcomes if future.exception() is not None]
        self.assertIsInstance(failures[0], ValueError)
        self.assertIn("conflicting roster marker", str(failures[0]))
        self.assertEqual(len(self.sheets.rows), 1)

    def test_lost_acknowledgement_can_be_reconciled(self):
        self.sheets.lose_response = True
        self.assertEqual(self.record(), {"status": "recorded"})
        self.assertEqual(len(self.sheets.rows), 1)

    def test_unavailable_reconciliation_reports_uncertainty_and_replay_is_safe(self):
        self.sheets.lose_response = self.sheets.lookup_outage = True
        with self.assertRaisesRegex(RuntimeError, "^Roster write unconfirmed"):
            self.record()
        self.sheets.lookup_outage = False
        self.assertEqual(self.record(), {"status": "recorded"})
        self.assertEqual(len(self.sheets.rows), 1)

    def test_outage_before_commit_can_be_replayed(self):
        self.sheets.before_commit_outage = True
        with self.assertRaisesRegex(RuntimeError, "^Roster write unconfirmed"):
            self.record()
        self.assertEqual(self.sheets.rows, [])
        self.sheets.before_commit_outage = False
        self.record()
        self.assertEqual(len(self.sheets.rows), 1)

    def test_changed_completion_or_target_tab_requires_reconciliation(self):
        self.record()
        with self.assertRaisesRegex(ValueError, "conflicting roster marker"):
            self.record(member={**MEMBER, "membership": "abroad"})
        self.sheets.tab = 1
        with self.assertRaisesRegex(ValueError, "conflicting roster marker"):
            self.record()
        self.assertEqual(len(self.sheets.rows), 1)

    def test_foreign_metadata_is_preserved(self):
        marker = {"metadataId": 87, "metadataKey": "other", "metadataValue": "other", "location": {"sheetId": 0}}
        self.sheets.markers[87] = marker
        with self.assertRaisesRegex(ValueError, "conflicting roster marker"):
            self.record()
        self.assertEqual(self.sheets.markers[87], marker)
        self.assertEqual(self.sheets.rows, [])

    def test_bad_document_or_member_never_opens_sheet(self):
        for document_id in (None, True, 0, -1, 2**31, "87"):
            with self.subTest(document_id=document_id), self.assertRaises(ValueError):
                self.record(document_id=document_id)
        for fields in ({"name": ""}, {"email": "bad"}, {"membership": "unknown"},
                       {"installments": True}, {"installments": "2"}, {"extra": "value"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.record(member={**MEMBER, **fields})
        self.auth.assert_not_called()

    def test_missing_or_ambiguous_header_rejected_before_write(self):
        for headers in (self.sheets.headers[1:-1], self.sheets.headers + ["email"]):
            self.sheets.headers = headers
            with self.assertRaisesRegex(ValueError, "required header"):
                self.record()
        self.assertEqual(self.sheets.writes, 0)

    def test_read_errors_do_not_expose_transport_details(self):
        self.sheets.read_outage = True
        with self.assertRaisesRegex(RuntimeError, "^Could not read the roster\\.") as error:
            self.record()
        self.assertTrue(error.exception.__suppress_context__)
        self.assertNotIn("private transport detail", str(error.exception))
        self.assertEqual(self.sheets.writes, 0)

    def test_retry_is_bounded_and_suppresses_private_errors(self):
        self.sheets.before_commit_outage = self.sheets.lookup_outage = True
        with self.assertRaisesRegex(RuntimeError, "^Roster write unconfirmed") as error:
            self.record()
        self.assertTrue(error.exception.__suppress_context__)
        self.assertNotIn("private transport detail", str(error.exception))
        self.assertEqual(self.sheets.writes, 1)

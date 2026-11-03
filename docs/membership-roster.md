# Replay a membership roster write

The Windmill script `f/Membership/record_completed_membership` records one mapped Documenso completion in Google Sheets. It is a separate, opt-in roster step with no trigger or email action. The existing membership automation still uses its original Sheets and mail steps.

An authorized caller supplies the original positive numeric Documenso document ID, the target spreadsheet ID and tab, a protected Google service-account resource, and a member with four fields:

```json
{"name":"Example Member","email":"sample@example.invalid","membership":"standard","installments":2}
```

Use one Documenso instance and one dedicated spreadsheet for this roster. Reserve the positive document IDs for its receipt metadata. The tab must contain exactly one header for each of `name`, `email`, `membership`, and `installments`, in any order. Extra headers are allowed. The supported membership choices are `standard`, `rca`, `abroad`, and `sophomore`; installments must be 1 or 2. Member strings are written as literal cell values.

A single [Sheets batch update](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate) appends the row and creates a [developer metadata](https://developers.google.com/workspace/sheets/api/guides/metadata) entry whose unique ID is the document ID. Its value is a digest of the four fields, and its location identifies the tab. Concurrent copies cannot both commit that ID. If a request fails or its response is lost, the script checks the marker. An exact match confirms the write. A conflicting marker requires an operator to reconcile the document and roster. An unreadable or absent marker leaves the outcome unconfirmed; restore Sheets access and replay the **same** document ID and field values. Every invocation attempts at most one batch and one reconciliation lookup.

The marker records a committed operation. Subsequent manual row edits or deletion do not remove the marker. Protect the tab and its header layout from concurrent manual edits; the batch does not lock the header read. Deleting the tab also deletes its metadata. Protect the metadata along with the roster, observe Google's per-spreadsheet metadata limits, and retain both in recovery procedures. Do not switch spreadsheets, clear a marker, or choose a new ID to bypass an error.

## Reproduce the contract tests

From the Womb root, run the following. It uses the same pinned Python packages as the Windmill script. The simulated Sheets transport keeps synthetic data in process and cannot open network sockets to Google.

```sh
direnv exec . env UV_CACHE_DIR="$PWD/.devenv/membership-uv" uv run --no-project \
  --with-requirements windmill/f/Membership/merge_records_to_google_sheet.script.lock \
  python -m unittest discover -s tests/membership
```

GitLab runs these tests with Python 3.12, matching the Windmill lock. The concurrency guarantee derives from the documented unique metadata ID and atomic Sheets batch; the synthetic transport checks the client protocol, not the real provider implementation.

## Cutover prerequisites

Verify the protected signing template, final signer, field mappings, authenticated webhook route, spreadsheet ownership, job access and retention with the operator. The roster script trusts its caller to authenticate and map the completed document; the live HTTP path has not been tested. The preserved preprocessor omits raw request logging. Review the deployed job’s logging and prior log retention before enabling production traffic. Rehearse a duplicate and a lost acknowledgement against a disposable spreadsheet, including marker backup and recovery. Existing rows written by the old script have no marker. Pause old writes and reconcile the historical rows before selecting this step for new completions. Never backfill by replaying the legacy history directly. Payment email delivery, its own delivery ledger, and the eventual flow switch remain separate work under [issue 5](https://gitlab.com/terrace/tech/womb/-/issues/5).

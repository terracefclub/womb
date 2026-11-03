# Preservation, migration, and recovery

Recorded 2026-10-27. No production migration or restore has been performed.

## WordPress

The public site includes posts, newsletter PDFs, events, menus, donations,
merchandise, shows/audio, contact, initiatives and a mentorship pilot. Public
pages cannot establish preservation of drafts, submissions, plugin tables,
subscription consent or provider configuration.

Obtain an encrypted full database and file backup (uploads, plugins, themes,
configuration) through a protected channel. WXR omits options and media binaries:
[WP-CLI export](https://developer.wordpress.org/cli/commands/export/). Keep exports
and personal records outside Git with restricted access.

Inventory post types/statuses, taxonomies, authors, timestamps, slugs, URLs,
redirects, attachments/checksums, PDFs/audio, forms/submissions, plugin tables,
calendar recurrence/timezones/exceptions, menus, signup consent and integrations.
Reconcile record counts and media hashes. Stable source IDs make imports safe
to repeat.

Preserve donation/merchandise provider relationships, recurring-payment ownership,
receipts, purpose designations and history. Payment details stay at their
provider. Test with sandboxes, no real charges/messages. Use approved legal/tax
wording, not wording inferred from nonprofit status.

Keep WordPress intact until isolated restore, import reconciliation, URL checks,
editor review and rollback rehearsal pass. Decide content-freeze/final-delta
strategy before DNS cutover. Enable CDN after origin acceptance.

## Membership flow

Windmill receives Documenso completions, extracts member/membership/installment
choices, and branches into a Google Sheets update and an email containing a
QuickBooks payment link. Preserve the sheet schema/ownership, service-account
permissions and downstream contracts.

The [opt-in roster step](membership-roster.md) can reconcile a repeated or interrupted Sheets write using the Documenso document ID. The live flow still needs contract verification and cutover.

Use synthetic identities and an email sink to test:

1. Missing/invalid authentication, unexpected event types and malformed schemas.
2. Required fields and valid membership/installment choices.
3. Deduplication using a stable Documenso event/document identifier.
4. Idempotent roster updates and concurrent-delivery races.
5. Separate email delivery status, preventing duplicate messages when a sheet
   write retries and allowing failed emails to be replayed.
6. Bounded backoff/retries, rate limits, safe replay and actionable failures
   without member data in logs.
7. Out-of-order events, invalid sheet headers, provider outages and interruption.

Map and rotate legacy credentials with live consumers before migration. Back up
Windmill state and verify recovery before changing its encryption key; changing
the key alone can make stored resources unreadable. Never commit exported keys
or credential values. Removing a file does not remove public history.

## Recovery

- Git, artifacts, bootstrap access and encrypted backups must remain reachable
  after total server loss. Recovery material must not depend on that server/SSO.
- Use database-consistent backups, not copies of live database directories.
  Include CMS uploads/config; explicitly exclude caches and rolling NVR footage.
  Retain important Frigate clips/exports separately.
- Restore to isolated databases/volumes with email/payment actions disabled.
  Check records, relationships, media, logins and workflows.
- Reprovision from the repo, restore, validate, then switch traffic. Prevent the
  old server from continuing writes.
- Proposed content RPO: 24 hours; proposed recovery: same day with an operator.
  Measure these targets before treating them as guarantees.
- A single node has maintenance/failure downtime. Backups and CDN do not make
  CMS writes, the community or on-premises devices highly available.

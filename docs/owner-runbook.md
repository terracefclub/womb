> This is a sanitised snapshot of the owner's private working copy of this runbook (2026-11-18, Womb `01464be`, website `dev` `dad0a20`). Machine-specific paths and private file names are replaced by placeholders, and it may lag the working copy. `$TERRACE_ROOT` stands for the root of the owner's workspace.

# Terrace revival: owner runbook

Prepared 2026-11-18, independently verified the same day (see the Verification log at the end), from `CLAUDE_HANDOFF.md` (website checkpoint 2026-11-18, Womb checkpoint 2026-11-13), Womb `origin/trunk` `01464be` and tfc-website `origin/dev` `8883875`, then re-checked against `dad0a20` and lease 4's result (second pass). Nothing in this file is new procedure. Every command is copied from a repository document, and the source is given in parentheses; where a command was adapted (a placeholder, a port or a directory chosen for you) the step says so. Where no procedure is written down, the step says **GAP: no documented procedure** and says what is missing.

---

## Part 0. How to use this runbook

Work top to bottom. Tick the box when the "Worked if" line is true. Do not skip a part because a later part looks more fun: Part 3 (preserve WordPress) comes before anything that can change data.

### What each step shows

- **Who**
  - **You**: only you can do it (accounts, money, people, provider consoles).
  - **Claude can do this for you**: ask Claude in a session. Anything that needs the Linux builder (Falcon, the Linux builder named in the handoff) or a Hetzner lease is in this group. Nix, VM and release builds are never run on your Mac.
  - **Needs your decision**: Claude can prepare options, but the choice is yours.
- **Status** (what exists today, with evidence)
  - **PROVEN ON HETZNER**: ran on real Hetzner servers (probes 1 to 4, probe 2 run twice, and known-full leases 1 to 4; every one tore down cleanly with the baseline preserved; handoff Womb checkpoints). Lease 1 stopped at the app plan, lease 2 at FORMAT, lease 3 at the services launch after install, reboot and networking, and lease 4 at the first plan step on builder-lock contention. Only what lease 3 (run `90e965da`) reached counts for install, boot and networking.
  - **NOT YET PROVEN ON HETZNER**: attempted in leases 3 and 4, not reached. Lease 3 stopped at the services launch (stale provider binding, fixed offline). Lease 4 (run `7d217921`, 2026-11-18) stopped at the first plan step because a non-Terrace job took Falcon's shared build lock; it proved nothing new beyond lease 3. Do not treat as proven; it is also not known to be broken.
  - **PROVEN IN VM REHEARSAL**: passed on the Falcon builder in disposable QEMU guests with synthetic data (`docs/full-rehearsal.md` and the per-service docs).
  - **PROVEN LOCALLY**: ran on a Mac or loopback stack with synthetic data (handoff website section, website repo `docs/local-preview.md`), or, for release packaging, on the Linux builder with loopback only. A recorded real-browser or live-stack run is required; tests alone are not enough.
  - **TESTED IN CODE ONLY**: unit or integration tests exist (including tests against a real local PostgreSQL or a fake Keycloak API); no recorded run of the real flow end to end.
  - **NOT YET BUILT**: nothing exists, or no procedure is written. Used together with **GAP** when there is no documented procedure.
- Part 1 is a list of inputs, not steps, so it has no status. The step counts reported with this file cover Parts 2 to 10.

### The three environments (and a fourth, later)

| Environment | What it is | Data | Who touches it |
| --- | --- | --- | --- |
| Local preview on your Mac | Loopback-only Keycloak, CMS and website started by `./tools/local-stack start` (website repo `docs/local-preview.md`). Website `http://127.0.0.1:4194/`, CMS `http://127.0.0.1:3040/admin/login`, Keycloak `http://127.0.0.1:8186/admin/`. | Synthetic only | You |
| Disposable Hetzner rehearsal | Two leased servers (app and relay) created, installed, tested and deleted in one run. Driven by private tooling on the builder and `docs/cloud-rehearsal.md`. Names are synthetic `.invalid`. Each run needs your fresh explicit yes. Its planner waits on Falcon's shared build lock, which other jobs also use (G-42). | Synthetic only | Claude, with your yes |
| Production | Does not exist yet. No production host profile, no real DNS, no real data on any server. | Real, later | Claude and you |
| On-prem (later) | One x86_64 server at the club. Hardware unknown. | Real, later | You (hardware), Claude (software) |

### Safety rules (five lines)

1. Never paste a secret, token, password, export or member list into chat, an issue, a pull request or a command argument. Refer to files by path.
2. Do not touch the existing Hetzner server, the live WordPress site or public DNS until the step says so. Do not delete or resize anything without a verified backup.
3. Every Hetzner lease needs your fresh explicit yes. Nothing may be left running after a run.
4. Use synthetic data until the roster, approvers and moderator (Part 1) are named. No real invitations, emails, payments or signing requests before that.
5. Never widen a setting to "make setup easier": no public registration, no plain HTTP, no skipping migrations, no `--allow-nonlocal-database` without a reviewed reason.

---

## Part 1. Things only you can provide

Tick each when done. "Unblocks" says what waits on it. "Default now" is what the code or docs assume until you decide.

- [ ] **1.1 Full Kinsta backup ZIP** (database and files).
    - Why: WordPress content export (WXR) has no media bytes, menus or plugin settings. The ZIP is the recovery copy and the source for images, newsletter PDFs and menus.
    - Unblocks: Parts 3 and 4 (media bytes, 12 newsletters, events and initiatives with images, menu structure, redirect plugin rules), the isolated restore, cutover.
    - Default now: not supplied. `web/tools/wp-import/zip-hooks.ts` reports `todo-kinsta-zip`. (website repo `docs/wordpress-import.md`, handoff)
- [ ] **1.2 Settings that Kinsta keeps outside the ZIP**: redirects, custom Nginx rules, IP-deny rules, DNS records and TTL, domains and TLS, scheduled jobs, cache/CDN exclusions, access restrictions.
    - Why: the ZIP excludes MyKinsta settings and custom server configuration. (website repo `docs/wordpress-parity-plan.md`, "Close the full-backup gap")
    - Unblocks: redirect parity at cutover, DNS cutover.
    - Default now: none captured.
- [ ] **1.3 Stripe account** for the receiving organisation, with test Payment Links for each fund and frequency, receipts and subscription management enabled, and your approved donation wording.
    - Why: the site only hands off to Stripe-hosted links. No account is connected. (`web/docs/donations.md`)
    - Unblocks: live donations. Existing recurring gifts stay at the old provider; nothing migrates automatically.
    - Default now: `DONATIONS_MODE=disabled`; the established Terrace donation page remains.
- [ ] **1.4 Dedicated Hetzner project credential and access recovery.**
    - Why: production must live in the dedicated Terrace project, never the personal homelab project. A project-scoped Read & Write token is kept as a protected file in the credentials folder outside every checkout (reference only; you confirmed its use on 2026-11-07 for the rehearsals). You also still need Console access to the dedicated project, and the old server's SSH key is unknown ("we might have to regenerate this").
    - Unblocks: every Hetzner lease, the real production servers, inventory of the existing server.
    - Default now: a read-only inventory ran on 2026-11-07 (one running CPX11 in Ashburn, two primary IPs, one SSH key). The existing server is protected and outside every write target. (handoff "Authorized Hetzner cloud rehearsal progress"; `docs/access-relay.md` "Open prerequisites")
- [ ] **1.5 Domain and DNS registrar access.** Access to the registrar/DNS for `terracefclub.org` (infrastructure domain). The handoff and docs do not say who controls DNS for `princetonterraceclub.org` (the current public WordPress site); record that too.
    - Why: certificates, hostnames and cutover.
    - Unblocks: Part 8.
    - Default now: no DNS changed. Only `headscale.terracefclub.org` and `vaultwarden.terracefclub.org` are named in the docs. No public hostname for the new website is chosen (see 1.19).
- [ ] **1.6 Real roster of current members and alumni.**
    - Why: it is the reference for approving applicants. Note: the system has no bulk invite (see gap G-17); applicants apply one by one at `/apply/`.
    - Unblocks: real invitations.
    - Default now: none; synthetic accounts only. (`docs/manual-steps.md`, website repo `docs/member-lifecycle.md`)
- [ ] **1.7 Approver names and approval policy**: who approves, the fallback approvers, whether editors may approve or only administrators, what verification the club expects, and whether alumni and current members need different evidence.
    - Default now in code: editors and administrators can both review and decide applications; the policy wording is a placeholder. (website repo `docs/member-lifecycle.md`, "Permissions summary")
- [ ] **1.8 Moderation owner and a backup**, response targets and an appeal process.
    - Default now: none; reports are queued for editors and administrators. Needed before real invitations. (website repo `docs/member-lifecycle.md` section 6)
- [ ] **1.9 Primary and backup identity administrator** for production Keycloak, plus the named list of editors and which of them are `editorial-admin`.
    - Why: no shared administrator session is acceptable; accounts are per person. (`docs/identity-host.md`, `cms/README.md`)
    - The editors list is not named as an owner item in the docs; it is added here because accounts cannot be created without names.
- [ ] **1.10 Retention decisions** (all current numbers are placeholders):
    - how long to keep moderation content, reports and their notes
    - tombstones of erased members and audit rows
    - the message outbox (it holds invitation links)
    - whether an erased founder's group name and description are kept (default: kept verbatim)
    - Keycloak data and logs (`docs/identity-host.md`: "approved retention policy")
    - Source: website repo `docs/member-lifecycle.md` sections 8, 9 and "Decisions the owner still owes".
- [ ] **1.11 Lifecycle numbers**: invitation lifetime (default 7 days, `LIFECYCLE_INVITATION_DAYS`), offboarding and moderation grace period (default 30 days, `LIFECYCLE_GRACE_DAYS`), pending application age-out (default 60 days, `LIFECYCLE_APPLICATION_DAYS`).
- [ ] **1.12 Mail delivery choice.** There is no mail transport anywhere today.
    - What it affects: invitations and notices (today an operator copies a link from an outbox file); Keycloak verification and password-setup mail (the production realm contains no mail settings); Documenso signing mail (local sink only); the membership workflow's payment email.
    - Default now: outbox rows plus a local file mirror, delivered by a person. (website repo `docs/member-lifecycle.md` sections 3, 4; `identity/README.md`; `docs/documenso-host.md`)
- [ ] **1.13 Gravity Forms keep or drop**, form by form. Seven form definitions (97 fields) were supplied: contact, newsletter contribution, donation and merchandise are the visible ones. Historical entries are already excluded by you.
    - Default now: no form is rebuilt. The only new intake form is `/apply/`. (website repo `docs/wordpress-parity-plan.md`)
- [ ] **1.14 Off-site backup accounts**: a Hetzner Storage Box sub-account (SFTP key, verified host key) and optionally a Backblaze B2 bucket and key. Choose where the repository passwords are escrowed, in a password manager that does not depend on Terrace servers. Name an owner for every backed-up dataset.
    - Default now: none exists; every owner is "TBD". (`docs/backups.md`, `docs/manual-steps.md`)
- [ ] **1.15 Hosting size and budget confirmation.** Total recurring hosting must stay strictly below US$80 per month including backups, tax and overlap with the old server. Provide the account currency, VAT status, the existing server's itemised invoice and authenticated stock.
    - Default now: the plan in `docs/access-relay.md` (option A: relay CX23 in hel1, app CX43 in nbg1, about $37.18 steady, $44.77 overlap month). The rehearsals ran CPX22 and CPX42 instead (see gap G-9). On 2026-10-31 Hetzner's public plan pages showed every CX, CAX and CPX plan as "currently unavailable", a weak signal: the rehearsals created CPX22 and CPX42 servers anyway, and CX stock for this project has never been checked.
- [ ] **1.16 Decision on the existing Hetzner server** (Ashburn CPX11): inventory, protected copy, and later retire or delete. Never resize it (a rescale reprices it).
- [ ] **1.17 Existing automation access**: endpoints and operator access for the live Windmill, Documenso and Vaultwarden, the Google Sheets roster and its service account, and the QuickBooks payment-link email step. Also who owns the Google Drive documents (menus) and the Google Calendar sync.
    - Why: the membership workflow must be preserved, and legacy source exposed credential material that must not be rotated until every consumer is mapped and a recoverable backup exists. (`docs/migration.md`, handoff "Other missing inputs")
- [ ] **1.18 Hardware inventory for the on-prem server** (list in Part 10).
- [ ] **1.19 Public hostnames and certificate approach** for: public website, CMS editor, identity, Headscale, Vaultwarden, Documenso, Windmill. Default: only `headscale.terracefclub.org` (relay default) and `vaultwarden.terracefclub.org` (legacy declaration) are named.
- [ ] **1.20 Fresh yes for each Hetzner lease.** Lease 4 has ended without reaching the services (see 6.6). A fifth lease needs a new, explicit yes and the lock fix first (the open question about letting follow-on leases run without asking each time is still unanswered in the handoff).

---

## Part 2. See it working on your Mac

All synthetic. Run it only while you are watching, then stop it. Do not leave it running. Written from website repo `docs/local-preview.md`, `identity/README.md`, `cms/README.md`, `web/README.md` and website repo `docs/member-lifecycle.md` at website `dev` `dad0a20` (first checked at `8883875`).

Where you type: every command below goes into Terminal on your Mac, **from the top folder of the website checkout** (the folder that contains `tools/`, `web/`, `cms/` and `identity/`), unless a step says otherwise. Step 2.2 creates that checkout and 2.3 onwards assume you are inside it. Do not type anything that prints a password, and never paste one anywhere.

- [ ] **2.1 Check the tools exist.** Nix, devenv and direnv must be available.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT as a documented check
    - GAP: no documented procedure. Website repo `docs/local-preview.md` only says "with Nix, devenv and direnv available" and that nothing is installed in your user profile; no command to check them is written anywhere (the three version commands in the earlier draft were not in any source and were removed). Do not install anything yourself: project rules forbid user package installs. Needed: Claude confirms all three work on this Mac before you start.
    - Worked if: Claude tells you all three are present.
- [ ] **2.2 Get a clean checkout of the website at `dev`, in a short path.** The owner's older website checkout is on an old feature branch, so do not use it.
    - Who: Claude can do this for you (the commands are yours to run if you prefer)
    - Status: PROVEN LOCALLY (the worktree commands are the workspace standard in `WORKER_RULES.md`; the local stack ran from such worktrees)
    - Do (adapted: `tfc-preview` and `codex/tfc-preview` are names chosen here; pick other names if either exists):
    ```bash
    git -C $TERRACE_ROOT/.repos/tfc-website.git fetch origin
    git -C $TERRACE_ROOT/.repos/tfc-website.git worktree add $TERRACE_ROOT/worktrees/tfc-preview -b codex/tfc-preview origin/dev
    cd $TERRACE_ROOT/worktrees/tfc-preview
    ```
    Keep the checkout path short. `tools/local-stack` cannot create its PostgreSQL socket if the path is too long (handoff, PR9 notes: over 103 bytes). Computed from `cms/tools/with-local`, the socket file is the checkout path plus 42 bytes, so the checkout path itself must be at most 61 bytes; the path above is 53.
    - Worked if: `git log -1 --oneline` in the checkout shows `dad0a20` or a later `dev` commit.
- [ ] **2.3 Approve the three shells once.** Inside the checkout:
    - Who: You
    - Status: PROVEN LOCALLY (website repo `docs/local-preview.md`)
    - Do:
    ```bash
    direnv allow identity
    direnv allow cms
    direnv allow web
    ```
    - Worked if: no error. (Nothing is installed in your user profile.)
- [ ] **2.4 Start the stack.** Inside the checkout:
    - Who: You
    - Status: PROVEN LOCALLY (website repo `docs/local-preview.md`)
    - Do:
    ```bash
    ./tools/local-stack start
    ```
    The first start downloads the pinned Keycloak release and installs the pinned CMS and website dependencies, so it takes a while; let it finish. It starts a local PostgreSQL on a private socket, applies the CMS migrations, then starts the CMS and website detached. If another preview already uses the ports (the defaults are website 4194, CMS 3040, Keycloak 8186 and its management port 9186), choose other ones before the first start; the chosen ports are remembered and later commands reuse them:
    ```bash
    ./tools/local-stack start --identity-port 8190 --cms-port 3046 --web-port 4252
    ```
    (With these alternative ports the three addresses below change to 4252, 3046 and 8190.)
    - Worked if: it prints the three URLs, the synthetic usernames and credential references, and never passwords. If it fails, it prints "Local stack command failed" and leaves its logs in the component `.state/local-stack.log` files; ask Claude to read them, because they are private.
- [ ] **2.5 Check health.**
    - Who: You
    - Status: PROVEN LOCALLY (website repo `docs/local-preview.md`)
    - Do:
    ```bash
    ./tools/local-stack status
    ```
    - Worked if: the exit status is 0 (all three services healthy; type `echo $?` straight afterwards to see it). A non-zero exit means at least one is not healthy.
- [ ] **2.6 Find the synthetic sign-in names and where their passwords are.** Open `identity/.state/credentials.json` yourself in an editor. Do not paste the values anywhere.
    - Who: You
    - Status: PROVEN LOCALLY (website repo `docs/local-preview.md`)
    - Names and keys:

    | Purpose | Username | Key in the file |
    | --- | --- | --- |
    | Current member | `local-member` | `memberPassword` |
    | Alumni member | `local-alumni` | `alumniPassword` |
    | CMS editor | `local-editorial-editor` | `editorialEditorPassword` |
    | CMS administrator | `local-editorial-admin` | `editorialAdminPassword` |
    | Keycloak admin | `terrace-local-admin` | `adminPassword` |

    The identity README also lists `local-unapproved` (key `outsiderPassword`), which has no approved role and is for checking the denial screen.
    - Worked if: you can see the keys. The file is mode 0600 in a 0700 directory.
- [ ] **2.7 Look at the public site in sample mode.** Open `http://127.0.0.1:4194/`.
    - Who: You
    - Status: PROVEN LOCALLY (`web/README.md`)
    - Worked if: the home page loads. Events are the three labelled fictional ones (Midnight Soup Symposium, Feedback for Houseplants, Pool Table Peace Talks). The footer has a "News" link (it shows nothing useful until step 2.10).
- [ ] **2.8 Look at newsletters and search.** Open Newsletter, open an issue at `/newsletter/<issue>/`, type in the search box.
    - Who: You
    - Status: PROVEN LOCALLY (`web/README.md`, handoff 2026-10-31)
    - Worked if: all four curated issues are readable page by page, and search shows page numbers with snippets. Search needs JavaScript. The text is machine OCR, not a corrected transcription. These four issues are built into the site; they are not the 12 newsletters in the WordPress export.
- [ ] **2.9 Sign in to the CMS admin as the editor.** Open `http://127.0.0.1:3040/admin/login`, choose **Sign in with Terrace**, use `local-editorial-editor` and its password from 2.6.
    - Who: You
    - Status: PROVEN LOCALLY (`cms/README.md`, `identity/README.md`; the handoff records Chromium sign-in against a real local Keycloak)
    - Worked if: you reach the Payload dashboard. Among the collections you should see Events, Newsletters, Initiatives, Media, Redirects, Posts and Pages, and the "Member lifecycle" and "Community moderation" groups.
- [ ] **2.10 Put synthetic news and pages on the site.** A fresh CMS has no published content. Apply the invented fixture plan with publishing on, to the local preview database only. The stack from 2.4 must still be running.
    - Who: Claude can do this for you (or You)
    - Status: PROVEN LOCALLY (handoff PR10 note: the worker applied the fixture with `--publish` in a local stack and saw 200 and 301 responses). The exact command below is assembled from the documented flags in website repo `docs/wordpress-import.md` and `cms/tools/wp-import.ts`; no source gives a local-stack walkthrough.
    - Do: make an owner-only folder outside every Git checkout (the tool refuses a folder inside one; the `install -d -m 0700` form is borrowed from `docs/full-rehearsal.md`, the website docs only say to use an owner-only directory), then run the apply from the checkout's `cms/` folder. Replace `/absolute/private/dir` with the real path, for example a new folder in your home directory:
    ```bash
    install -d -m 0700 /absolute/private/dir
    cd cms
    direnv exec . tools/with-local bun tools/wp-import.ts apply --plan test/fixtures/wp-import-plan.synthetic.json --report /absolute/private/dir/fixture-apply.json --publish
    cd ..
    ```
    (The report file must not already exist; it is created exclusively with mode 0600.)
    - Worked if: the terminal prints aggregate counts only. In the website at `http://127.0.0.1:4194/`, switch the masthead to **View published site** (the button then reads **View sample site**), then open `/news/`. Drafts and unknown paths return the same 404. Old WordPress paths in the fixture redirect with a 301.
- [ ] **2.11 Look at the member area.** Use a private browsing window, or sign out of Terrace first: the editor session from 2.9 lives in the same Keycloak, and an account without a member role sees the denial screen (it has a switch-account button, handoff note on TFC !21). At `http://127.0.0.1:4194/`, choose **Members** and sign in as `local-member`, then as `local-alumni`, then try `local-unapproved`.
    - Who: You
    - Status: PROVEN LOCALLY (handoff 2026-11-04 captures and real-browser sign-in; `identity/README.md`)
    - Worked if: member and alumni reach "The living room", stories, people, mentorship and profile pages. The unapproved account sees the approval screen. Profile directory and mentorship are off by default.
- [ ] **2.12 Try the account page.** As a member, open `/members/account/`.
    - Who: You
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: real-browser member profile, story, report, export and erasure against a local stack). It is unverified on any deployed host. Expect rough edges and report them.
    - Worked if: you can download your own data as JSON. Do not press the final "erase my data" confirmation unless you want that synthetic member erased.
- [ ] **2.13 Walk the application lifecycle once, locally.**
    - Who: You (Claude can drive it)
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: real-browser apply, approve, invite, redeem, provision and first sign-in with real Keycloak SSO). Not a production procedure. The 2026-11-18 defects in this path (class-year field, unstyled pages) are fixed on `dev` `dad0a20` by website PR 13.
    - Do:
        1. Open `http://127.0.0.1:4194/apply/`, submit a made-up name and an `.invalid`-style address. (Submissions faster than 3 seconds after the page loaded are rejected as spam: wait a few seconds.)
        2. In the CMS admin (signed in as in 2.9) open **Member applications**, pick the role, set status `approved`.
        3. Read the one-time link from the outbox mirror file in `cms/.state/outbox/` (named `<id>-<kind>.json`). Do not paste it anywhere. (The outbox rows visible in the admin never contain the link.)
        4. Open the link (`/join/#<token>`) and press **Accept invitation**.
        5. Create the sign-in account, from the checkout's top folder. `KEYCLOAK_HTTP_PORT` must be your identity port: 8186 if you used the default ports in 2.4 (the source document now uses 8186 too; 8190 is the alternative port from 2.4):
        ```bash
        KEYCLOAK_HTTP_PORT=8186 direnv exec identity bun identity/tools/provision-member.ts provision --username verify.alumna --email person@example.org --name "Verify Alumna" --role alumni
        ```
        6. The temporary password is in `identity/.state/provisioned/<username>.txt` (mode 0600), never printed. Sign in with it at the website's **Members** link; Keycloak forces a change.
    - Worked if: the account reaches the member area. (website repo `docs/member-lifecycle.md`, "Verify")
- [ ] **2.14 Offboard it.** Create a **Member offboardings** record for that member, then close the realm side from the checkout's top folder. GAP: the admin screens cannot create this record yet (the rehearsal had to use the CMS REST API; fix in progress, G-41), so ask Claude to create it as the administrator `local-editorial-admin` (editors cannot offboard):
    - Who: You
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: offboard and restore, through the REST API for the CMS side; not a production procedure)
    - Do (same port rule as 2.13):
    ```bash
    KEYCLOAK_HTTP_PORT=8186 direnv exec identity bun identity/tools/provision-member.ts offboard --username verify.alumna
    ```
    - Worked if: the member can no longer reach `/members/`. Their profile, stories and groups are hidden but kept.
- [ ] **2.15 Stop the stack.**
    - Who: You
    - Status: PROVEN LOCALLY (website repo `docs/local-preview.md`)
    - Do (with the same port flags if you used them in 2.4):
    ```bash
    ./tools/local-stack stop
    ```
    - Worked if: it reports each managed service stopped and the credentials, database files, media and logs retained. A non-zero result means something could not be verified safe to stop: ask Claude to inspect; do not kill processes by pattern.

---

## Part 3. Preserve WordPress before anything else

Rule: keep WordPress intact until an isolated restore, import reconciliation, URL checks, editor review and a rollback rehearsal have all passed. (`docs/migration.md`)

- [ ] **3.1 Download the Kinsta backup ZIP.** Kinsta allows one download backup per week and keeps it for 48 hours, so download it the moment it is ready.
    - Who: You
    - Status: NOT YET BUILT (a provider screen, not a repo tool). Provider path as recorded in website repo `docs/wordpress-parity-plan.md` (checked 2026-11-04).
    - Do: in MyKinsta choose **Sites, then the site, then Backups, then Download, then Create backup now**, and download the ZIP. If a final WP Engine backup still exists, keep it too as extra evidence.
    - Worked if: you have one ZIP. Write down the site and environment, the backup timestamp and timezone, the download date and the byte count.
- [ ] **3.2 Store it safely.** Put the untouched ZIP in an owner-only directory outside every Git checkout (for example the owner's private folder outside every checkout, which already holds the WXR export), mode 0600 on the file. Keep a second protected, recoverable copy somewhere else.
    - Who: You
    - Status: NOT YET BUILT (the docs require this, but give no command for the second copy or where it lives)
    - GAP: no documented procedure for the second copy or its location. Needed: your choice of the second place, and who can read it.
    - Worked if: two copies exist and the first is not group- or world-readable.
- [ ] **3.3 Check the ZIP without opening it.**
    - Who: Claude can do this for you (You can run it too)
    - Status: TESTED IN CODE ONLY (nine synthetic checks; merged in tfc-website PR 7; not run on a real ZIP because none exists yet)
    - Do: from the repository root:
    ```bash
    direnv allow web
    direnv exec web python3 -B web/tools/kinsta-backup-preflight.py /protected/terrace/kinsta-backup.zip
    ```
    (website repo `docs/wordpress-parity-plan.md`, "Check a downloaded ZIP before recovery")
    - Worked if: it prints only counts, byte sizes and the archive SHA256. It confirms a nonempty SQL file beside `public/` and a nonempty `public/wp-load.php`, and reports how many files are under `public/wp-content/uploads/`. Save that receipt with the ZIP. It does not prove the SQL is consistent or the files complete.
- [ ] **3.4 Export what the ZIP leaves out**: redirects, custom Nginx rules, IP-deny rules, DNS records with TTL, domains and TLS, scheduled jobs, cache/CDN exclusions, access restrictions.
    - Who: You
    - Status: NOT YET BUILT
    - GAP: no documented procedure. Website repo `docs/wordpress-parity-plan.md` lists what to capture ("Export redirects separately; capture exact host/path/query behavior...") but gives no screens, commands or file format. Needed: a checklist of MyKinsta and DNS-provider screens, where to store the output, and how to turn the redirect list into the importer's redirect format. See G-1.
    - Worked if: a private ledger holds each item. Store secret references, never values.
- [ ] **3.5 Inventory plugins and integrations**: active, inactive and must-use plugins with versions, themes, membership/access rules, Gravity Form IDs and conditional logic, notifications, add-on feeds, SMTP, payment and webhook configuration, Drive/document/calendar ownership.
    - Who: You (with Claude reading the exported SQL later)
    - Status: NOT YET BUILT
    - GAP: no documented procedure or tool. The parity plan states the requirement. The importer's SQL hooks (`DatabaseTableProvider.pluginOption`) are typed stubs. Needed: an allowlisted reader for plugin options in the SQL dump and a template for the ledger. See G-2.
    - Worked if: every public clue (Divi, Gravity Forms, Stripe, The Events Calendar, FlowPaper, Google Drive, Yoast, Snazzy Maps, Akismet) has an owner and a keep/drop mark. Public plugin clues are not proof of settings.
- [ ] **3.6 Inventory the Gravity Forms definitions.** You already supplied a form-definition export (not entries).
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (run on the supplied JSON; the parity plan records seven forms, 97 fields, nine notifications and seven confirmations)
    - Do:
    ```bash
    direnv exec web bun web/tools/gravity-forms-inventory.ts < /protected/terrace/forms.json
    ```
    - Worked if: you get one JSON report by form and field ID with counts only. No labels, recipients or submissions appear. Entries stay excluded; do not purge them from any backup.
- [ ] **3.7 Restore the ZIP in isolation and compare.** Only into a separate restricted environment, with outbound email, payment and webhook callbacks, cron and external integrations disabled before any code runs. Check tables and counts, uploads and PDF bytes with hashes, representative layouts, and which pages are really public versus private.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: no documented procedure or tooling. The parity plan has a requirements table only. Needed: an isolated WordPress/MariaDB runtime (nothing is built), a written restore recipe, and a comparison report format. See G-2.
    - Worked if: counts and hashes match the preflight receipt and a person signs off.
- [ ] **3.8 Re-run the WXR preflight on the supplied export.** The protected WXR and Gravity Forms JSON are in the protected export folder outside every checkout (reference only).
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (real export, 2026-11-03; reproduced exactly 2026-11-18)
    - Do:
    ```bash
    direnv exec web bun web/tools/wxr-preflight.ts < /protected/terrace/wordpress.xml
    ```
    Add `--plan /protected/terrace/wxr-review-plan.json` before the input redirect to write the record-by-record checklist (new file, mode 0600, outside Git).
    - Worked if: 245 item records and 87 publish-status candidates, unchanged. The 87 are candidates only; effective public access (membership or password plugins) is unproven.
- [ ] **3.9 Inventory and protect the existing Hetzner server** (Ashburn CPX11, in the existing Womb project as last seen; the project and server identifiers are deliberately left out of this public-repository copy). Approved access, services, databases, DNS dependencies, credential consumers, a protected copy with a proven isolated restore, and a rollback path. Do not resize it.
    - Who: Needs your decision, then Claude
    - Status: NOT YET BUILT
    - GAP: no documented procedure beyond the principle in `docs/access-relay.md` ("Open prerequisites" 3) and `infra/cloud/README.md`. Its contents and billing are unknown. SSH on port 22 was reachable and 80/443 were not on a past check; there is no working key. Needed: access recovery (1.4), then an inventory checklist. See G-3.
    - Worked if: a private inventory and a restored copy exist.
- [ ] **3.10 Decide the content freeze and final delta.**
    - Who: Needs your decision
    - Status: NOT YET BUILT
    - GAP: `docs/migration.md` says to "decide content-freeze/final-delta strategy before DNS cutover" and gives no strategy or tool. Needed: how editors keep publishing on WordPress during the migration and how new or changed items are re-imported (the importer is repeatable by WordPress ID, which supports a delta, but no cutover procedure uses it). See G-4.
    - Worked if: a dated plan exists: freeze time, who stops editing, the final export, the final apply.

---

## Part 4. Migrate content

What the tooling does today (dev `dad0a20`): turn the WXR into a private plan, report on it, and write Payload documents as drafts into a local database. It has never been applied to a production database. A local rehearsal (handoff, real-export local rehearsal 2026-11-18, worktree at `8883875`, local database only) imported the owner's real export: 215 documents created with 0 failures, a re-apply that wrote nothing, a rollback to zero, then `apply --publish` published 56 documents and activated 51 redirects. A crawl found all 38 posts and 16 pages returning 200 and 50 of 51 redirect sources answering 301 to a 200, with no 5xx. Everything for production remains undone from step 4.6.

Real-export dry-run results to compare against (2026-11-18; handoff and website repo `docs/wordpress-import.md`): 245 items, 87 publish candidates, 215 mapped (122 media references, 41 posts, 25 pages, 14 events, 12 newsletters, 1 initiative), 30 unmapped (11 venues and organizers embedded in events, 11 menu items, 4 Divi layouts, 3 style records, 1 event with no start date), 86 redirects (80 redirect, 6 served in place) with 2 gaps (site root and one reserved-route collision), 56 publish-eligible. Website repo `docs/wordpress-import.md` now states the same 86 and 2.

Use `PRIV` for an existing owner-only (0700) directory outside every Git checkout and `EXPORT` for the protected WXR. Every `--report` and `--out` path must be a new file: the tools create reports exclusively at mode 0600 and refuse to overwrite, so give every run its own file name. Plan and report commands that use `cms/tools/wp-import.ts` run from the `cms/` folder of a source checkout, because the release bundle does not contain them (G-5).

- [ ] **4.1 Dry run on the real export.** Writes only a safe report.
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (real export, a safe aggregate report kept in the protected export folder, mode 0600)
    - Do:
    ```bash
    direnv exec web bun web/tools/wp-import/import.ts dry-run --report $PRIV/wp-import-dryrun-DATE.safe.json < $EXPORT
    ```
    - Worked if: the counts above reproduce and the report contains no titles, bodies, names or emails.
- [ ] **4.2 Create the plan** (private file with titles and bodies; treat it like the export itself).
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: planner on the real export gave 245 items, 215 mapped, 86 redirects, 56 publish-eligible). The tool refuses `--out` inside a Git checkout, `.state/` included; use the private directory.
    - Do:
    ```bash
    direnv exec web bun web/tools/wp-import/import.ts plan --out $PRIV/wp-import-plan.json --report $PRIV/plan-report.json < $EXPORT
    ```
    - Worked if: both files exist at mode 0600. Never edit a plan; re-run the planner instead (the plan hash is an integrity check, not authenticity).
- [ ] **4.3 Dry run against a local CMS** (the stack from Part 2 running, migrations applied).
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (synthetic fixture; and the real plan against a local database in the real-export rehearsal, handoff, real-export local rehearsal 2026-11-18)
    - Do:
    ```bash
    cd cms
    direnv exec . tools/with-local bun tools/wp-import.ts dry-run --plan $PRIV/wp-import-plan.json --report $PRIV/cms-dryrun.json
    ```
    - Worked if: the report lists what apply would create, update or skip. Nothing is written.
- [ ] **4.4 Read the reconciliation report.** Check each line:
    - Who: You
    - Status: PROVEN LOCALLY (report format)
    - Look for:
        - source counts equal the preflight (245 and 87)
        - every unmapped item has a reason you accept: `embedded-in-events-not-standalone`, `menus-need-database-backup-or-review`, `divi-layout-library-not-imported`, `theme-styles-not-imported`
        - review flags: `unsafe-html-stripped`, `builder-code-dropped`, `form-shortcode-dropped`, `plugin-shortcode-dropped`, `event-date-review`, `event-timezone-review`, `event-venue-missing`, `event-end-invalid`, `media-bytes-pending`, `divi-layout-review`, `alt-missing`, `slug-deduplicated`
        - redirect gaps: `redirect-site-root`, `redirect-reserved-route`, `redirect-query-or-fragment-route`, `redirect-foreign-origin`, `redirect-unrepresentable-path`, `redirect-duplicate-source-path`
    - Worked if: you can say, for every non-zero line, "accepted", "fix in the editor" or "wait for the ZIP".
- [ ] **4.5 Rehearse apply as drafts on the local preview.** Everything is created as a draft unless `--publish` is given.
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: 215 created, 0 failed, re-apply a no-op; local database only, never production)
    - Do:
    ```bash
    cd cms
    direnv exec . tools/with-local bun tools/wp-import.ts apply --plan $PRIV/wp-import-plan.json --report $PRIV/cms-apply.json
    ```
    - Worked if: counts match the dry run; a second identical run (give it a new report file name: reports are never overwritten) writes nothing (idempotent); the documents appear as drafts in the admin and the public site shows none of them. Use the local database for the rehearsal. The tool refuses a non-local database unless `--allow-nonlocal-database` is passed.
- [ ] **4.6 Apply the real plan to the production CMS as drafts.**
    - Who: Needs your decision, then Claude
    - Status: NOT YET BUILT
    - GAP: no documented procedure. The release bundle contains only the migrator (`bin/migrate`), not the importer. The importer runs from a source checkout through `tools/with-local` and refuses non-local databases (loopback allowed, so an SSH tunnel counts as local; the flag "guards intent, not topology"). There is no production host yet and no written way to run it against one. See G-5.
    - Worked if: the production report matches the rehearsal counts.
- [ ] **4.7 Import media bytes, newsletter PDFs and menus from the Kinsta ZIP.**
    - Who: Claude can do this for you, after 1.1
    - Status: NOT YET BUILT
    - GAP: only typed stubs exist (`MediaBytesProvider.find()`, `DatabaseTableProvider.menus()`, `pluginOption()`; they report `todo-kinsta-zip`). Needed: the ZIP, a reader for uploads and the SQL dump, a navigation collection for the 11 menu items, and SHA-256 reconciliation of originals and derivatives. Until then no Media can be public and no newsletter, event poster or initiative image can publish. 18 referenced media IDs and 28 body URLs are missing from the export. See G-6 and G-7.
    - Worked if: every Media reference has verified bytes and the `media-bytes-pending` flag clears.
- [ ] **4.8 Review flagged items visually.** 24 items are flagged `divi-layout-review` (Divi layouts were flattened to text and basic HTML). Compare each with the live WordPress page.
    - Who: You (editors)
    - Status: NOT YET BUILT
    - GAP: there is no draft preview for any collection (stated in website repo `docs/wordpress-import.md` at `dad0a20`). The existing preview toggle only switches sample versus published content, and drafts return 404 on the public site. Editors can only read the fields in the Payload admin. Needed: a draft preview route and an owner decision on how editors authenticate to the website, or a staging copy of the site. Known defect from the rehearsal: nine near-empty pages and one raw `[tt-event]` shortcode were publish-eligible, so review those before publishing (G-41). See G-8.
    - Worked if: each reviewed item is marked fixed or accepted. Also fix `alt-missing` media alt text in the admin.
- [ ] **4.9 Publish.** Two ways: add `--publish` to the apply run (only documents that run creates, only those marked eligible, and never anything with a blocking flag), or have an editor publish in the admin. Updates to an already-published document are written as a draft version, so the public page never changes by itself.
    - Who: You (editors), Claude can run the apply
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: `apply --publish` published 56 documents; a real browser passed editor publish and unpublish; 38 of 38 posts and 16 of 16 pages returned 200). Known defect: `apply --publish` publishes nothing on an already-imported set (G-41).
    - Worked if: the page opens at `/news/<slug>/` or `/pages/<slug>/` and the same slug while still a draft returns 404.
- [ ] **4.10 Activate redirects.** The importer stores 301 rows with `active: false` until their target is published. Re-run apply with the same plan after publishing; a later apply run activates the redirect once the document is published.
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: 51 redirects active, 50 of 51 sources answered 301 to a 200). Known defect: publishing in the admin does not activate a redirect until `apply` is re-run, as this step says (G-41).
    - Do:
    ```bash
    cd cms
    direnv exec . tools/with-local bun tools/wp-import.ts apply --plan $PRIV/wp-import-plan.json --report $PRIV/cms-apply-2.json
    ```
    - Worked if: opening an old WordPress URL in a browser lands on the new path with a 301, and an old URL whose target is still a draft does not redirect. Changes can take up to five minutes to appear (in-process cache). Rules held only by Kinsta or Nginx are not part of this (G-1).
- [ ] **4.11 Roll back an import run** if something is wrong.
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: rollback to zero verified; tests also cover the refusal paths)
    - Do:
    ```bash
    cd cms
    direnv exec . tools/with-local bun tools/wp-import.ts rollback --run wpimport-YYYYMMDDHHMMSS-abcdef
    ```
    - Worked if: the run is marked `rolled-back` and created documents are gone. Rollback refuses (changing nothing) when: a later run depends on the document (roll back newest first), an editor edited or published it (`--discard-edits` overrides), media is referenced elsewhere, or the run is still `running`. A crashed apply is marked `failed` and can be rolled back. A run stuck `running` needs `--take-over-stale` after one hour idle.
- [ ] **4.12 Rebuild approved forms** (contact, newsletter contribution, donation, merchandise, as you decided in 1.13).
    - Who: Claude can do this for you, after 1.13
    - Status: NOT YET BUILT
    - GAP: no form submission collection or endpoint exists besides `/apply/`. Needed: the keep/drop decision, dedicated staff-only submission collections, validation, spam and rate controls, safe uploads and duplicate-safe notifications (all described as design gates, none built). See G-29.
    - Worked if: each kept form works against a sandbox and sends no real message.
- [ ] **4.13 Decide where events come from.** The public Events Calendar returned zero events and the export has 14 mapped events; Google Calendar sync and Drive documents were clues only.
    - Who: Needs your decision
    - Status: NOT YET BUILT
    - GAP: no verified calendar source and no procedure to preserve or replace Google Calendar sync or the Drive menu documents. See G-30.
    - Worked if: you can say which system is the source of truth for events and menus after cutover.
- [ ] **4.14 Reconcile after the real apply.** Account for all 245 records, every media original and derivative, and every post-export change, by stable WordPress ID and hash.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: counts reconcile in reports, but media hash reconciliation and the final delta need 4.7 and 3.10. See G-4, G-6.
    - Worked if: a signed reconciliation sheet explains every exclusion.

---

## Part 5. Identity and members

The member system is a dedicated Terrace Keycloak with approval required for every account. It runs only locally today. Production Keycloak has never been started with real accounts.

- [ ] **5.1 Name the identity administrators and the editors** (1.9).
    - Who: Needs your decision
    - Status: NOT YET BUILT (an owner input; no code needed)
    - Worked if: two named administrators (primary and backup) and the editor list exist in a private record.
- [ ] **5.2 Generate the production realm file** (user-free: four application clients, no users, no admin client, no mail settings). Use real HTTPS origins, and independently generated client secrets delivered from a secret manager. Never reuse the local `credentials.json`.
    - Who: Claude can do this for you
    - Status: TESTED IN CODE ONLY (contract tests; the opt-in native import test needs the pinned archive). Womb's gateway check uses the same contract files hash-pinned at TFC `3902e16`; see G-13.
    - Do: (`identity/README.md`)
    ```bash
    umask 077
    mkdir -m 700 /protected/terrace-identity
    direnv exec identity bun identity/tools/production-realm.ts \
      --web-origin https://www.example.org \
      --cms-origin https://editor.example.org \
      --output /protected/terrace-identity/terrace-realm.json \
      < /protected/terrace-client-secrets.json 2>/dev/null
    ```
    Replace the example hosts with the approved real origins. The secrets file must contain exactly `webSecret`, `introspectionSecret`, `cmsSecret` and `editorialSecret`.
    - Worked if: the command is silent and a new 0600 file exists. Record the source revision and checksum in protected storage (the README gives the exact `git rev-parse` and `sha256sum` lines).
- [ ] **5.3 Deliver the realm file and secrets to the identity host** as root-only files (realm file, database password, TLS certificate and key).
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: no production secret delivery exists (sops-nix or agenix is not adopted; "Production secret delivery is still a release gate"). Rehearsal delivery over pinned SSH works only for disposable hosts. See G-11.
    - Worked if: the files exist on the host at mode 0400, owned by root, outside the Nix store.
- [ ] **5.4 Start Keycloak on PostgreSQL** using the native identity module (Keycloak 26.8.1, backend and management on loopback, nginx limited to the Terrace realm, public resources and discovery).
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (`docs/identity-host.md`, 145 s two-VM check; also part of the all-services rehearsal). No production host exists (G-10).
    - Do: on the host, as root, check readiness:
    ```bash
    systemctl show keycloak.service postgresql.service terrace-identity-health.service -p ActiveState -p SubState -p Result -p ExecMainStatus
    systemctl start terrace-identity-health.service
    ```
    - Worked if: the units are active and the health unit succeeds. The realm import creates a missing realm and skips an existing one; it is not reconciliation. Never delete a realm to force an update (G-39).
- [ ] **5.5 Get into the production Keycloak admin console.**
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: no documented procedure. The proxy denies the root, administration and master-realm paths and "no remote admin route is provided yet". The backend is on loopback port 8088 and management on 9000. `docs/identity-host.md` does not say how an operator opens the admin console (an SSH local forward is how the K3s API and Home Assistant are reached, but it is not written for Keycloak). Needed: the procedure and a bootstrap administrator that is removed afterwards. See G-15.
    - Worked if: a named administrator signs in over a private path and the temporary bootstrap account no longer exists.
- [ ] **5.6 Create editor accounts and grant roles.** CMS access comes only from the realm roles `editorial-editor` and `editorial-admin`; member approval grants none. A removed role takes effect within 60 seconds; revoke the session for immediate effect.
    - Who: You, using the Keycloak admin console
    - Status: NOT YET BUILT (production)
    - GAP: no documented procedure beyond "grant/remove roles in Keycloak" (`cms/README.md`). Locally the synthetic editors are seeded. Verified-email and first-password handling have no mail transport behind them (G-16, G-20).
    - Worked if: each editor signs in at the CMS admin with **Sign in with Terrace** and sees only what their role allows.
- [ ] **5.7 Set the production environment so applications can open.** For the website and CMS (delivered as protected env files):
    - Who: Claude can do this for you
    - Status: TESTED IN CODE ONLY (website repo `docs/member-lifecycle.md`, `cms/README.md`, `web/README.md`). Not run on any deployed host.
    - Required values (names only; never print values):
        - CMS: `LIFECYCLE_INTAKE_SECRET` (32+ chars; unset means `/apply/` and `/join/` show "not open" and the endpoints answer 503), `LIFECYCLE_OUTBOX_DIR` (absolute, outside the code and media directories; a public CMS refuses to start without it), `LIFECYCLE_PUBLIC_ORIGIN` (a bare public https origin; a public CMS refuses to start without it), `CMS_ORIGIN`, `CMS_PUBLIC_DEPLOYMENT=1`, the CMS and member auth settings, `CMS_MEDIA_DIR`, `DATABASE_URI`.
        - Website: `ADDRESS_HEADER` (a header the proxy overwrites with the real client address) together with `XFF_DEPTH`, or `LIFECYCLE_DIRECT_CLIENTS=1` if there is no proxy; `/apply/` stays closed otherwise. Also `ORIGIN` equal to `MEMBER_AUTH_ORIGIN`, `CMS_ORIGIN`, and `CMS_PUBLIC_ORIGIN` (without it every inline body image is dropped and the CSP names no media origin).
        - Omit every `CMS_LOCAL_BOOTSTRAP` and `CMS_BOOTSTRAP_*` key.
    - GAP (reduced): website PR 11 added one "Production configuration" table to website repo `docs/member-lifecycle.md` listing every CMS and website variable, what happens when it is unset, and that the website logs a `[config]` line (names only) per missing setting. The Womb side is still open. That table names `ADDRESS_HEADER` with `XFF_DEPTH` set to the number of trusted proxies for `X-Forwarded-For`. Womb's gateway code (`platform/website-gateway.nix`) sets `X-Forwarded-For` to `$remote_addr`, one hop, so `X-Forwarded-For` with depth `1` matches by reading the code, but no Womb document says so, no test checks the pairing, and no Womb module sets `ADDRESS_HEADER`, `XFF_DEPTH`, `LIFECYCLE_*` or `CMS_PUBLIC_ORIGIN` at all. See G-14.
    - Worked if: `/apply/` opens, approval succeeds, and a spoofed `X-Forwarded-For` from a client does not choose its own rate-limit bucket.
- [ ] **5.8 Open applications.** Share the `/apply/` address with people on the approved roster. Applications stay pending until reviewed; at most 500 pending are held, pending applications are withdrawn after `LIFECYCLE_APPLICATION_DAYS` (60 by default), and a staff notice is added when the queue passes 400.
    - Who: You
    - Status: TESTED IN CODE ONLY for production; PROVEN LOCALLY for the form (handoff, real-export local rehearsal 2026-11-18)
    - Worked if: a test application from the real address appears in the admin under **Member applications**. Use your own address, not a member's.
- [ ] **5.9 Approve or deny.** In the admin open **Member applications**, choose the role to grant (`current-member` or `alumni`), set status `approved` or `denied`, add a note if needed. The decision is final and approving does not create a sign-in account.
    - Who: You (the approvers you named in 1.7)
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: real-browser approve with real Keycloak SSO)
    - Worked if: an approval creates an invitation record and an outbox row; a denial adds a denial notice.
- [ ] **5.10 Deliver the invitation.** The one-time link exists only in the outbox mirror file `LIFECYCLE_OUTBOX_DIR/<id>-<kind>.json` (folder mode 0700, files 0600). Pass it to the applicant privately. The link is `/join/#<token>`, valid `LIFECYCLE_INVITATION_DAYS` (7 by default).
    - Who: You
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: invite and redeem in a real browser)
    - GAP for production: there is no mail transport and no documented way to read the file on a server. Website PR 11 documents the directory (absolute, outside the code and media directories; the CMS and the sweep must run as the same account, because the folder is mode 0700) and makes a public CMS refuse to start without it. Still open: the production location, a check that the service can write it, and backup inclusion (it holds live invitation links). The sweep now retries missing mirror files and lists an invitation whose file is missing as `mirror.undeliverable`: reissue it. See G-19, G-20.
    - Worked if: the applicant opens the link and presses **Accept invitation**, and the token cannot be used a second time.
- [ ] **5.11 Create the person's Keycloak account** and grant exactly one member role.
    - Who: You, in the Keycloak admin console
    - Status: NOT YET BUILT (production)
    - GAP: `identity/tools/provision-member.ts` refuses anything but the loopback local realm. For production the doc says to follow "the same steps" in the admin console and lists production enrollment, verified contact ownership and password delivery as open. The realm requires verified email, and no SMTP exists. Needed: a written console procedure (user, verified-email flag, member role, temporary password and forced change) and a private way to deliver the temporary password. See G-16.
    - Worked if: the person signs in once at the website and reaches the member area. First sign-in of a freshly provisioned user has not been verified in a browser.
- [ ] **5.12 Link the member** to their application and invitation (administrator only, after first sign-in; today through the CMS REST API, G-41). This lets that member read those two records.
    - Who: You
    - Status: PROVEN LOCALLY through the REST API only (handoff, real-export local rehearsal 2026-11-18). GAP: the admin screens cannot link a member yet (fix in progress, G-41).
    - Worked if: the member row shows on the application. Link before any erasure, so the erasure can find the application.
- [ ] **5.13 Offboard a member.** (a) An administrator creates a **Member offboardings** record (GAP: the admin screens cannot do this yet, so it goes through the CMS REST API, G-41). The member is refused (401 and 403) even if Keycloak still lets them sign in, their content is hidden but kept, and a notice is queued. (b) Close the realm side.
    - Who: You
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: offboard and restore, CMS side through the REST API, because the admin screens cannot create an offboarding yet, G-41). Production realm step is a GAP (G-16).
    - Do (local realm only; use your own identity port, 8186 by default):
    ```bash
    KEYCLOAK_HTTP_PORT=8186 direnv exec identity bun identity/tools/provision-member.ts offboard --username verify.alumna
    ```
    - Worked if: the member roles are removed, the account is disabled and live sessions ended. Within the grace period (`LIFECYCLE_GRACE_DAYS`, 30 by default) setting the state to `restored` re-activates the member; re-run `provision` for the realm. After it, restore is refused. Nothing is deleted automatically.
- [ ] **5.14 Handle a moderation report.** Members can report a story, a group (and group posts) and a mentorship message. In **Moderation reports** an editor or administrator sets `hidden`, `dismissed` (restores from hidden) or `resolved`. Hidden content is hidden from everyone, including its author, but kept as evidence.
    - Who: You (the moderator you named in 1.8)
    - Status: PROVEN LOCALLY (handoff: TFC !35 integrated browser run through real member and editorial Keycloak; `cms/tools/test-reports-browser.ts`)
    - Worked if: the status changes and the content disappears from member views. Reporter identity is not shown to editors. No response-time or appeal process exists until you set one (1.8).
- [ ] **5.15 Member data export.** A signed-in member downloads their own JSON at `/members/account/` (5 per hour, never emailed or stored, each list capped at 2000 records).
    - Who: You (members do this themselves)
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: export in a real browser). Never run on a deployed host.
    - Worked if: the file contains only that member's records and a hidden-by-moderation flag, no other people's writing.
- [ ] **5.16 Erase a member.** Member-initiated: **Start erasure** gives a single-use 15-minute code, then they type "erase my data". Administrator-initiated: only for an already offboarded member, by creating a **Data requests** record of kind `erasure`. Run the realm offboard step afterwards.
    - Who: You
    - Status: PROVEN LOCALLY (handoff, real-export local rehearsal 2026-11-18: erasure in a real browser). Known defect: erasure leaves personal data when the application was never linked to the member, so link first as the next paragraph says (G-41).
    - Before you start: link the application to the member, and check no other applicant shares the contact address (erasing one person also anonymises other unlinked applications and outbox rows that use the same address).
    - Worked if: the member row is a tombstone (status `erased`, no roles, name and subject wiped) and a confirmation is queued.
- [ ] **5.17 Run the cleanup sweep after every administrator-initiated erasure.** It also expires stale invitations, withdraws unreviewed applications, completes pending mirror redaction, and reports content past its grace period. With `--purge-moderated` it deletes moderation-hidden content in one transaction. Offboarded members are never erased automatically.
    - Who: Claude can do this for you
    - Status: TESTED IN CODE ONLY (tests; no recorded run against a live local stack)
    - Do:
    ```bash
    cd cms && direnv exec . tools/with-local bun run lifecycle:sweep
    cd cms && direnv exec . tools/with-local bun run lifecycle:sweep -- --purge-moderated
    ```
    - GAP for production (reduced): the sweep is still not in the release bundle and nobody has scheduled it. Website PR 11 documents a production invocation from a source checkout of the deployed revision with the CMS runtime environment, `cd /path/to/checkout/cms && bun run lifecycle:sweep`, recommends once a day at a quiet hour (for example 03:30) without `--purge-moderated` until you set retention (1.10), and says Womb must own the timer and its service account. Needed: a packaged `bin/sweep` and the Womb timer. See G-18.
    - Worked if: the JSON report lists nothing pending, `mirror.failed` is 0 and `mirror.undeliverable` is empty (reissue any listed invitation), and the confirmation file is written once and not recreated if you delete it.
- [ ] **5.18 Bring in the real roster.**
    - Who: Needs your decision
    - Status: NOT YET BUILT
    - GAP: no bulk invitation or import path exists. Applications can only be created by the public `/apply/` form (not even staff can create one), capped at 500 pending. A real roster of current members and alumni can only be used to check applicants by hand. Needed: a decision between "everyone applies" and a bulk-invite tool. See G-17.
    - Worked if: you can describe, in one sentence, how a person on the roster ends up with an account.

---

## Part 6. Deploy the platform

### What the platform is

- A pinned NixOS flake (`platform/`) for one x86_64 host. K3s (single node, SQLite) is kept for later workloads, with its API closed at the firewall and a `local-retain` storage class. It does not join or span the homelab cluster, and there is no high availability. A one-node outage is downtime.
- The website, CMS, Keycloak, Documenso, Windmill and Vaultwarden run as separate native or Podman services on that host with native PostgreSQL. Each is an optional module, disabled by default, and no host profile enables any of them yet (G-10).
- A second, small host runs only Headscale and its embedded DERP relay, in a different data centre, so that access survives an application-host outage.
- Home Assistant and Frigate are bounded synthetic demonstrations only until the on-prem server exists.
- Backups are encrypted restic repositories off-host; Git is the source of truth for configuration.
- Budget rule: everything together strictly below US$80 per month. Internal rehearsal planning caps: at most $5 per run and under $20 per month in aggregate (`docs/service-lifecycle.md`), inside the $80 limit; the owner's 2026-11-04 direction says not to block work on cost analysis.

### 6A. What Hetzner has proven, in order

- [ ] **6.1 Know what is already proven on real servers.** Probes 1 to 4 (probe 2 ran twice) and known-full leases 1 to 4 (handoff Womb checkpoints, 2026-11-07 to 2026-11-18), each with the owner's yes. All of them tore down cleanly with the baseline preserved. Lease 1 stopped at the app plan, lease 2 at FORMAT, lease 3 at the services launch after install, reboot and networking, and lease 4 at the first plan step. What is proven on Hetzner:
    - Who: You (read and accept)
    - Status: PROVEN ON HETZNER (handoff "Resume checkpoint 2026-11-13" and its lease 3 record: `install` passed on both hosts (FORMAT, target copy, FINISH), `boot-installed` passed on both, and `repository-start` passed at 01:15:40; `docs/cloud-rescue-install.md`)
    - The list, in order:
        1. Create two servers (app CPX42, relay CPX22, nbg1) inside an armed expiring lease, with the original server outside every write target.
        2. Pin the Rescue host key independently (console fingerprint, no trust on first use).
        3. Boot the RAM installer (stock to RAM, UEFI confirmed on CPX22 and the CPX42 app type).
        4. Record the install plan from the measured disks (app `/dev/sda` 327,684,194,304 bytes, relay `/dev/sda` 81,923,145,728 bytes).
        5. Format, copy the Nix closure and install (`FORMAT`, store copy, `FINISH`) on both hosts (lease 3 `install`, app about 22 minutes, relay 8).
        6. Reboot into the installed disk and reach SSH on port 22 within about 9 seconds.
        7. Start the repository listener (lease 3 `repository-start`; in lease 4 the listener never started).
        8. Tear down: delete the servers, IPs, key and firewall, prove absence in three reads, preserve the baseline, remove the credentials copy.
    - Worked if: you accept it. Receipts are in the lease 3 run directory `cloud-90e965daf611c47b5a52fd666a996611` under the builder's work folder (reference only). Lease 4 repeated steps 1 to 3 (create, key pin, RAM installer, install-target check) and step 8 (teardown) on 2026-11-18 and stopped at step 4 for the lock reason in 6.6; the repository listener never started, and it adds no new proven step.
- [ ] **6.2 Next lease: services launch on both installed hosts.** Gateway bootstrap, 198 protocol assertions, relay nginx started with `LoadCredential`.
    - Who: Claude can do this for you (needs your yes for the lease)
    - Status: NOT YET PROVEN ON HETZNER: attempted in leases 3 and 4, not reached. Lease 3 stopped here because the provider binding was older than 120 seconds. Lease 4 stopped earlier, at the app plan step (6.6). PR45 added `require_launch_window` and the launcher refreshes both bindings (`cloud-controller.py binding --phase <phase> --role <role>`) right before launch. Proven offline only.
    - Worked if: Claude reports the supervisor passed `installed-boot-binding` and both roles' services came up. Do not call it proven until it reports so. The command text `cloud-controller.py binding --phase <phase> --role <role>` appears in the handoff only (a private operator script in the lease's proof directory); the repository tool behind it is `infra/cloud/rehearsal_apply.py binding <phase> --directory <run-dir> --credential-file <file> --role <role>`, and no step here asks you to run either.
- [ ] **6.3 Next lease: real off-host backup to Falcon** (restic REST over a pinned reverse SSH forward, then a full repository check and an encrypted copy).
    - Who: Claude can do this for you
    - Status: NOT YET PROVEN ON HETZNER: not reached in leases 3 and 4. The same path is proven offline for the relay on an OVMF fixture (`restic check --read-data` passed, restore reproduced both Headscale keys and an intact database).
    - Worked if: `restic check --read-data` passes with one snapshot per role.
- [ ] **6.4 Next lease: retire the original, then create, install and restore a replacement.** Original servers are deleted with an absence receipt, a fresh replacement cohort is installed, and each role restores into an empty disk.
    - Who: Claude can do this for you
    - Status: NOT YET PROVEN ON HETZNER: not reached in leases 3 and 4. The same sequence passed in VMs (see 6.7).
    - Worked if: restored data matches (database counts, file hashes, SQLite integrity, SSO login) and the originals are absent.
- [ ] **6.5 Final audit and teardown of any lease.**
    - Who: Claude can do this for you
    - Status: PROVEN ON HETZNER for the teardown and audit of a run that stopped early: every probe and lease 1 to 4 ended with absence in three reads and the baseline preserved. Not yet exercised after a complete run.
    - Worked if: zero servers, IPs, SSH keys or firewalls remain from the run; the baseline (one CPX11, two primary IPs, one SSH key) is unchanged; no token copy remains; the timer is inactive.
- [ ] **6.6 Lease 4 result, and the decision on a fifth lease.** Lease 4 (run `7d217921`, 2026-11-18 21:30 to 21:59 PST, CPX42 app and CPX22 relay in nbg1, at `01464be`) created both hosts, pinned the host keys without trusting first use, dispatched and verified the RAM installer on both and passed the app's install-target check again. It then stopped at the first plan step: a non-Terrace job took Falcon's shared build lock 5 seconds before the planner started and the planner's 100 second lock wait expired. No platform code was at fault. Everything was torn down and audited (absence in three reads, baseline preserved). It proved nothing new beyond lease 3.
    - Who: Needs your decision
    - Status: NOT YET PROVEN ON HETZNER (services launch, off-host backup, retire and replacement restore). A fix to the lease tooling (prebuild before arming, fair-queue lock waits, a time ledger) is in progress offline; see G-42.
    - Worked if: you have decided whether to approve lease 5 once the tooling fix is merged. Each lease needs a fresh "Yes, run lease N". The handoff's older 2026-11-18 resume checkpoint still says "Lease 4 (not started)"; the newer lease 4 checkpoint supersedes it (G-36).

### 6B. What the VM rehearsal proves (synthetic, on Falcon)

- [ ] **6.7 Know the all-services VM rehearsal result.** It composes K3s with PVC, website and CMS, Keycloak and PostgreSQL, Home Assistant and Frigate demos, Headscale with real Tailscale clients, signed Documenso, Windmill with the roster worker and Vaultwarden. It backs up quiescent state, retires every original guest, restores onto blank replacements, and re-runs two 198-assertion HTTPS and browser phases. Result: PASS in 1733.06 s at source `71b45be` (2026-11-05), 38 recorded process identities absent, synthetic state purged.
    - Who: You (read)
    - Status: PROVEN IN VM REHEARSAL (`docs/full-rehearsal.md`)
    - Does not prove: a blank provider install, real DNS and certificates, CDN, off-site recovery, real invitations, human relay enrollment, forced DERP, production sizing.
    - Worked if: you accept the limits.
- [ ] **6.8 Re-run the rehearsal** (for example after the website bundle changes). On Falcon, from a clean checkout:
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (`docs/full-rehearsal.md`, "All-services composition"). Archive names there are the 3902e16 bundles (G-13).
    - Do:
    ```bash
    install -d -m 0700 $HOME/terrace-work/full-rehearsal-output
    direnv exec . env BUILD_LOCK=$HOME/.falcon-heavy.lock \
      ./platform/full-rehearsal run --all-services \
      --output $HOME/terrace-work/full-rehearsal-output \
      --webArchive $HOME/terrace-work/release-390-output/terrace-web-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz \
      --cmsArchive $HOME/terrace-work/release-390-output/terrace-cms-3902e16c8c681638282e5983869d53efa8bb424c-linux-x64-gnu.tar.gz \
      --tfcSource $HOME/terrace-work/release-a2
    ```
    Purge only a completed, verified, synthetic run:
    ```bash
    direnv exec . ./platform/full-rehearsal purge $HOME/terrace-work/full-rehearsal-output/run-REPLACE_WITH_RECEIPT_RUN_ID --synthetic-only --yes
    ```
    - Worked if: the receipt shows `complete=true` and `computeOff=true`; all guests and processes are absent; purge removes only that run's disks, repository and escrow.

### 6C. Getting to a real production host

- [ ] **6.9 Choose the production servers and how to create them.**
    - Who: Needs your decision
    - Status: NOT YET BUILT
    - GAP: the repository has two unreconciled paths. (a) `infra/cloud` (Pulumi) declares relay `cx23`/`hel1` and app `cx43`/`nbg1` with a Debian bootstrap image and a budget guard (quote fields are filled from the account; apply is separate, targeted and reviewed; the guard runs through `infra/cloud/preview.sh`). Installing NixOS is "a separate, gated step". (b) The rehearsals created CPX22 and CPX42 and installed NixOS through Rescue and a RAM installer. CPX42 alone costs $81.99 per month, over the ceiling. `docs/service-lifecycle.md` says there is no standing CPX42 fallback. Hetzner's public plan pages showed every CX, CAX and CPX plan as unavailable on 2026-10-31, yet the rehearsals did create CPX servers, and CX stock for this project has never been checked. Needed: your sizing decision, an authenticated stock and price check, and one written production creation procedure. See G-9.
    - Worked if: you can name the two server types, regions, monthly total with tax and the one creation path.
- [ ] **6.10 Write the production host profiles** that enable the modules (website, gateway, identity, backup, relay, and later Documenso, Windmill, Vaultwarden) with real hostnames.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: every document says "actual host profiles remain disabled". Only rehearsal profiles exist (`infra/nixos/rehearsal-config.nix`, `platform/cloud-rehearsal/*`). The platform README warns the `hetzner.nix` BIOS profile is evaluation-only and must not be installed as-is. See G-10.
    - Worked if: a profile evaluates, builds on Falcon and boots in a VM with real module options.
- [ ] **6.11 Secrets delivery.** Each secret is a root-owned file, mode 0400, outside the Nix store and Git; Nix only carries paths.
    - Who: Claude can do this for you, with your values
    - Status: NOT YET BUILT
    - GAP: no production secret manager or generator or escrow retrieval exists ("Production secret delivery is still a release gate"; sops-nix not adopted). The files to deliver, from the docs: website `cms.env` and `web.env` (`/var/lib/terrace-website-inputs`, mode 0700 folder); realm JSON, database password and TLS for identity; backup repository passwords, SFTP key, known hosts and optional B2 environment file; TLS certificate and key for the gateway; Windmill `bootstrap.json`; Documenso `secrets.json`; Vaultwarden `input.json`. See G-11.
    - Worked if: each file exists at mode 0400, owned by root, regular (not a symlink), and a recovery copy is escrowed outside the servers.
- [ ] **6.12 Build release bundles for the website at the revision you will deploy** (dev `dad0a20` or newer). On Falcon, from a clean Linux checkout, with no application dotenv files, through the heavy-build queue:
    - Who: Claude can do this for you
    - Status: NOT YET BUILT for `dad0a20` (the procedure was run for `3902e16`, whose archives are on Falcon; `dev` has nine migrations and Next.js 16.4.5)
    - Do (`tools/release/README.md`):
    ```bash
    direnv allow web
    direnv allow cms
    ./tools/release/build /absolute/private/output
    ```
    - Worked if: the output has `terrace-{web,cms}-<revision>-linux-x64-gnu.tar.gz`, checksum sidecars, `SHA256SUMS` and `*.release.json`. Output is owner-only and existing artifacts are never overwritten. CI keeps artifacts seven days, so copy them out.
- [ ] **6.13 Verify and keep the bundles.**
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (on the Linux builder with loopback only, for the `3902e16` bundles; `tools/release/README.md`; not run for `dad0a20`, whose smoke check now compares the ledger with the migration registry instead of a fixed count). `./tools/release/smoke` is documented for a dedicated Linux checkout, never your Mac.
    - Do (`sha256sum -c` is in the README's prose; the smoke lines are its code block):
    ```bash
    sha256sum -c SHA256SUMS
    direnv allow cms
    direnv allow identity
    ./tools/release/smoke /absolute/private/output
    ```
    - Worked if: hashes match, and smoke passes: extracted bundle in an empty Bubblewrap filesystem, migrations twice with an unchanged ledger, editorial and member SSO, publish and withdraw, moderation, private media denied. Keep the archives and checksums outside expiring CI storage and keep the previous release beside the new one.
- [ ] **6.14 Re-pin Womb to the new bundles and re-run the website and gateway checks.** `platform/website-release.json` pins the `3902e16` archives (six migrations) and the gateway and identity checks hash-pin three TFC producer files from that revision. Dev (`dad0a20`) now has nine migration ledger rows, Next.js 16.4.5 and a changed lifecycle surface.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - Do (on Falcon, shared lock):
    ```bash
    BUILD_LOCK=/path/to/shared-heavy.lock platform/website-check \
      /protected/releases/terrace-web-<revision>-linux-x64-gnu.tar.gz \
      /protected/releases/terrace-cms-<revision>-linux-x64-gnu.tar.gz
    BUILD_LOCK=/path/to/shared-heavy.lock platform/website-gateway-check \
      /protected/releases/terrace-web-<revision>-linux-x64-gnu.tar.gz \
      /protected/releases/terrace-cms-<revision>-linux-x64-gnu.tar.gz \
      /source/tfc-at-<revision>
    ```
    (Argument shapes from `docs/website-host.md` and `docs/website-gateway.md`; they cannot pass until the pins are updated.) See G-13.
    - Worked if: both checks pass on the new archives.
- [ ] **6.15 Deploy a release: back up first, migrate explicitly, start.** Verify a recoverable backup and a retained previous bundle first. Migration stops both apps, applies the bundle's migrations and records the revision; it is never run at boot.
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (`docs/website-host.md`, 83.34 s two-guest check, 25 CMS tables, six-migration ledger of the `3902e16` bundle)
    - Do (as root on the host):
    ```bash
    systemctl start terrace-cms-migrate.service
    systemctl start terrace-cms.service terrace-web.service
    ```
    Repeat the migration action once to confirm the ledger is unchanged, then start both services again.
    - Worked if: startup accepts the revision marker and public content works while private routes stay denied. Code rollback means switching to the previous release root; it does not undo migrations. Database rollback is a separate restore from a verified backup with the matching uploads.
- [ ] **6.16 Gateway and TLS.** nginx gateway in front of the website and CMS on HTTPS 443 only, with distinct web and CMS hostnames, certificates delivered as systemd credentials, `private, no-store` on every response, nginx access logs off.
    - Who: Claude can do this for you; You (DNS and certificates)
    - Status: PROVEN IN VM REHEARSAL (gateway check: 197 protocol assertions plus a Chromium account-switch test, `docs/website-gateway.md` and handoff; the full rehearsal repeats 198; synthetic CA). Real certificates: NOT YET BUILT.
    - GAP: HTTP port 80 is closed, there is no HTTP-to-HTTPS redirect and no certificate acquisition or renewal ("separate activation work"). Needed: a certificate method (ACME or manual), renewal and expiry alerting. See G-12.
    - Worked if: the browser trusts the certificate, unknown hosts and mismatched SNI/Host get 421, spoofed forwarding is ignored, and identity admin paths are not reachable from outside.
- [ ] **6.17 Access relay (Headscale with DERP).** Independent host, deny-all peer policy by default.
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (`docs/access-relay.md`; real Tailscale clients, scoped sensor access and key recovery in the full rehearsal). The relay installed and booted from disk on Hetzner (lease 3); its services are NOT YET PROVEN ON HETZNER (leases 3 and 4 did not reach them).
    - GAP: human enrollment through Keycloak OIDC, WAN and forced DERP fallback, replacement of deny-all with reviewed grants, and a repo-local userspace Tailscale client are all unproven or unwritten. See G-34.
    - Worked if: an allowed operator path works, a denied member path fails, with real clients across a WAN.
- [ ] **6.18 Documenso** (signing). Official image v2.21.1, own PostgreSQL role, closed signup, approved accounts only (at most 32), public password routes blocked.
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (functional checks, synthetic signing, fresh-node recovery). Not deployed anywhere real.
    - Do (writers stopped, `docs/documenso-host.md`):
    ```bash
    sudo systemctl stop terrace-documenso.service
    sudo systemctl restart terrace-documenso-inputs.service
    sudo systemctl start terrace-documenso-database.service terrace-documenso-image.service
    sudo systemctl start terrace-documenso-migrate.service
    sudo systemctl start terrace-documenso-bootstrap.service
    sudo systemctl start terrace-documenso.service
    ```
    - GAP: there is no real mail transport (SMTP is a local sink; "signing delivery gate open") and no production certificate for signing. See G-25.
    - Worked if: an approved person signs in through Keycloak, an unsigned PDF can be created and fetched, and the readiness check returns 200.
- [ ] **6.19 Windmill.** Official image 1.861.0, one worker, per-user Keycloak sign-in, at most ten SSO users on the free edition.
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (runtime, individual SSO, session preservation, offline roster worker, replacement-host recovery)
    - Do (`docs/windmill-host.md`, which says to enable the module only in a disposable configuration with inputs and TLS already delivered): run `terrace-windmill-migrate.service` explicitly, then start `podman-terrace-windmill-server.service` and `podman-terrace-windmill-worker.service`; run `terrace-windmill-bootstrap.service` to repeat approval and provider reconciliation.
    - GAP: the live membership workflow (Documenso completion to Google Sheets and a QuickBooks payment-link email) is not migrated. The roster script is a separate opt-in step with no trigger, the live webhook path and authentication are untested, existing sheet rows have no markers and must be reconciled, and the Sheets, mail and payment contracts need your access (1.17). See G-26.
    - Worked if: an approved user signs in individually and a bounded Bash job runs. A generic job proves runtime only.
- [ ] **6.20 Vaultwarden.** Native 1.37.5, SSO only, `approvedEmails` is the complete allowlist, public `/admin` always denied.
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (item, attachment, renewal and revoked-renewal browser checks; combined recovery)
    - Do (`docs/vaultwarden-host.md`, inputs and certificates already installed):
    ```bash
    systemctl start terrace-vaultwarden-migrate.service
    systemctl start vaultwarden.service
    systemctl start terrace-vaultwarden-bootstrap.service
    ```
    - GAP: the existing live instance's data, endpoint and backups are uninspected, so migrating real vaults has no procedure. Server-side administrator readback does not decrypt anyone's vault. Store backup password escrow somewhere that does not live inside this Vaultwarden (circular). See G-27.
    - Worked if: an approved person signs in with SSO and unlocks their own vault with their master password.
- [ ] **6.21 Home Assistant and Frigate demos** (synthetic: six template sensors, a generated black camera stream, detection and recording off, loopback only).
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (nine scenarios, 317.38 s). No real devices anywhere.
    - Do (`docs/demos.md`):
    ```bash
    terrace-demo status
    terrace-demo teardown --yes
    ```
    `teardown --yes` stops the units and deletes all synthetic demo state: run it only when you want that. Reach it through an SSH local forward: `ssh -L 8123:127.0.0.1:8123 operator@HOST`.
    - Worked if: `terrace-demo status` shows both services within their limits. Real hardware is Part 10.
- [ ] **6.22 Measure the combined stack.** Required: at least 25 percent free memory and no out-of-memory events under representative load, before adding each of Documenso, Windmill and Vaultwarden, one at a time.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: only per-component measurements exist (for example CMS 101.9 MiB, web 47.3 MiB, PostgreSQL 121.2 MiB in a small synthetic run) and test-VM sizing; no combined production peak. See G-24.
    - Worked if: a recorded week of the combined workload shows the headroom.
- [ ] **6.23 Health, alerts and logs.** External probe, certificate expiry, capacity, backup age, bounded log retention.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: units fail visibly in `systemctl --failed`, but no alert delivery exists, identity logs are ephemeral under `/run/keycloak/` with no redactor for reading them, and application logs are volatile, size-bounded journals (8 MiB for the website module, 16 MiB for Windmill and Vaultwarden). See G-23.
    - Worked if: a deliberately broken check produces an alert you actually receive.

---

## Part 7. Backups and recovery

### What is backed up

Restic, client-side encrypted, to a Hetzner Storage Box (primary) and optionally Backblaze B2 (secondary). Retention per target: last 3 plus 7 daily, 4 weekly and 6 monthly (at most 21 snapshots). `restic check` with a 5 percent data subset runs Sundays 05:30. Backup runs daily at 03:15.

| Dataset | Method | Restore check |
| --- | --- | --- |
| CMS, community and Keycloak databases | `pg_dump -Fc` inside one REPEATABLE READ snapshot | row counts per table |
| PostgreSQL roles | `pg_dumpall --globals-only` | roles restored first |
| CMS and community media | restic files after the dump (default budget 5 GB) | sha256 and file count |
| Headscale SQLite and its Noise and DERP keys | online backup plus files | integrity check and key hashes |
| Website protected inputs, Documenso, Windmill and Vaultwarden databases and input folders | included automatically when `terrace.backup.enable` is set | per-service recovery checks |
| NixOS and Kubernetes config | Git | rebuild from a clean checkout |

Not covered: the K3s datastore and server token, local PV data, repository passwords, SFTP key and B2 keys (these are never in a backup), Home Assistant and Frigate real data and the message outbox folder (unless you add it to `extraPaths`).

- [ ] **7.1 Create the off-site accounts.** A Storage Box sub-account (SFTP key, host key verified out of band) and optionally a B2 bucket and application key. Created by you; credentials never go in chat.
    - Who: You
    - Status: NOT YET BUILT (an account step)
    - GAP: the Storage Box port, quota, host key and sub-account restrictions and the B2 S3 path were never tested against the real providers (the VM test used two SFTP repositories). See G-21.
    - Worked if: you hold the SFTP key, the pinned host key line and the bucket key in protected files.
- [ ] **7.2 Escrow the repository passwords.** One password per target so one leak does not open both. Losing every copy makes that repository unreadable forever.
    - Who: You
    - Status: NOT YET BUILT (no mechanism; owner action)
    - Worked if: you can retrieve each password without any Terrace server running and without Terrace SSO.
- [ ] **7.3 Name an owner for each dataset** (the table in `docs/backups.md`; every owner is "TBD").
    - Who: Needs your decision
    - Status: NOT YET BUILT
    - Worked if: each row has a named person.
- [ ] **7.4 Enable the backup module on a host and deliver the secret files** (mode 0400, root). Config example from `docs/backups.md`:
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (two-VM drill, 133 s, synthetic SFTP targets). No real target and no production secret delivery (G-11, G-21).
    - Do (verbatim from `docs/backups.md`; its `"community"` database name comes from the superseded HumHub plan: the accepted pilot keeps community data in the CMS database, so choose the real list when the host profile is written):
    ```nix
    terrace.backup = {
      enable = true;
      postgresql.databases = [ "cms" "community" "keycloak" ];
      media.cms-uploads = "/var/lib/payload/media";          # TBD real path
      targets.primary = {
        enable = true;
        repository = "sftp:uNNNNNN@uNNNNNN.your-storagebox.de:terrace-restic";
        passwordFile = "/run/secrets/terrace-backup/primary-password";
        sftp.identityFile = "/run/secrets/terrace-backup/storagebox-key";
        sftp.knownHostsFile = "/etc/terrace/backup/storagebox-known-hosts";
      };
    };
    ```
    The media path is a placeholder in the docs ("TBD real path"). Use one repository path per host.
    - Worked if: the module evaluates and tooling refuses group- or other-readable secret files.
- [ ] **7.5 Initialise each repository once, then take the first backup.** Initialisation is never automatic, so a wrong path cannot create an empty repository.
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL
    - Do (as root, per target):
    ```bash
    terrace-backup init primary
    systemctl start terrace-backup.service
    terrace-backup restic primary snapshots
    ```
    - Worked if: `snapshots` lists one snapshot and `terrace-backup age` shows a small age.
- [ ] **7.6 Tell whether backups are fresh.**
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (age signal, staleness flag, one failing target not blocking the other)
    - Do:
    ```bash
    terrace-backup age
    systemctl --failed
    ```
    Metrics are written to `/var/lib/terrace-backup/metrics/terrace_backup.prom` (`terrace_backup_last_success_timestamp_seconds`, `terrace_backup_last_run_ok` and others, per target). The hourly `terrace-backup-freshness` unit fails when a target is older than 26 hours or has never succeeded.
    - Worked if: `terrace-backup age` exits 0 (it exits 1 when stale or never run). Alerting on `time() - last_success > 93600` is not wired (G-23).
- [ ] **7.7 Run a restore drill into an isolated directory.** The restore script never touches live data: it restores into a new directory with a throwaway socket-only PostgreSQL cluster and reconciles against the embedded manifests. It sends no mail, makes no payment call and starts no application. Never restore over a live host to "test".
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (synthetic data). A real off-site drill from a different host and network has not been done.
    - Do (as root on a recovery host):
    ```bash
    terrace-backup restic primary snapshots                      # choose a snapshot ID
    terrace-backup-restore --target primary --snapshot <ID|latest> \
      --dest /var/tmp/terrace-restore-$(date +%Y%m%d) --keep-running
    terrace-backup-restore --verify-only <dest>
    terrace-backup-restore --stop <dest>
    ```
    Use `--target secondary` to test the second provider independently.
    - Worked if: the output ends `RECONCILIATION OK` (exit 0); the report is `<dest>/reconciliation.txt` (table names and counts only). `RECONCILIATION FAILED` means exit 1. Record the measured times: recovery time is unmeasured. Proposed targets are at most 24 hours of data loss and same-day recovery with an operator (`docs/migration.md`; `docs/backups.md` still says four hours, from the superseded community pilot), explicitly unmeasured. Repeat after identity data is real, using the identity recovery drill (`platform/check identity-restore`, `docs/identity-recovery.md`).
- [ ] **7.8 Put a recovery into service** (copy media back, load databases into the production cluster, restore Headscale's keys before its first start so clients keep their network identity).
    - Who: Needs your decision, then Claude
    - Status: NOT YET BUILT
    - GAP: `docs/backups.md` says this is "a manual, owner-approved step to be written and drilled once real hosts exist". The rehearsal does it for synthetic data on replacement guests (PROVEN IN VM REHEARSAL; leases 3 and 4 did not reach it on Hetzner), but no production runbook exists. See G-22.
    - Worked if: a written procedure exists and has been drilled on a replacement host with the original off.
- [ ] **7.9 Consistency window.** Each database is dumped in one snapshot, but there is no shared transaction across databases or between a database and media, so live writes can differ from a manifest. Production needs a quiescent window or storage snapshot.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: the rehearsal quiesces writers; the scheduled daily timer does not. Needed: a quiesce procedure and schedule for the real host. See G-21.
    - Worked if: a drill taken during writes reconciles.
- [ ] **7.10 Cover the K3s datastore and PV data**, or explicitly exclude them.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: `/var/lib/rancher/k3s/server/db`, the server token and PV data are not backed up by the module; the platform README keeps the restore gate open. Needed: add them to `extraPaths`/`media` once applications use PVs, or document that nothing durable lives there. See G-21.
    - Worked if: every persistent dataset is either backed up or declared disposable.
- [ ] **7.11 Provider image backups** (Hetzner, 20 percent of server price, 7 slots, disk-level and not database-consistent) as a second whole-host path.
    - Who: Needs your decision
    - Status: NOT YET BUILT
    - GAP: priced into option A but no restore procedure from a provider image is documented. Neither replaces the restic repositories or the drills. See G-21.
    - Worked if: you chose keep or drop, and a restore procedure exists if kept.
- [ ] **7.12 Preserve the WordPress and Kinsta copies and the release bundles** as separate backups: they are tracked in the TFC repository and Part 3, not by `terrace-backup`.
    - Who: You
    - Status: NOT YET BUILT
    - Worked if: the ZIP (two copies), the plan files, and the retained release archives with checksums exist outside CI storage.

---

## Part 8. Cutover

Do not start Part 8 until Parts 3 to 7 are ticked or each open item is accepted in writing. Keep WordPress and its rollback copy running until every gate below passes.

- [ ] **8.1 Run the pre-cutover release gates** (`docs/backlog.md`, "Release gates"). Report unit, integration, build, browser, deployment and restore evidence separately; static checks do not prove live health. Gate groups:
    - Who: Claude can do this for you; You sign off
    - Status: TESTED IN CODE ONLY for the automated parts; the live parts are NOT YET BUILT (G-38)
    - Gates:
        1. **Public routes:** semantic headings, keyboard access, mobile layout, no broken internal links, correct timezones, empty and error states, reduced motion.
        2. **Content:** drafts stay private, APIs enforce permissions, unsafe HTML rejected, uploaded media bounded, old URLs preserved.
        3. **Membership:** duplicate events create no duplicate records or messages, invalid authentication fails closed, bounded retries and visible replay status.
        4. **Operations:** dependency and image pins, resource bounds, probes, rollback, log redaction, backup-age alerts, restore evidence, access instructions.
        5. **Performance:** a representative US/mobile scenario with explicit budgets and recorded measurements; no promised score.
    - Commands that exist (from `web/README.md`, run from `web/`):
    ```bash
    direnv exec . bun run check
    direnv exec . bun test
    direnv exec . bun run build
    ```
    `web/tools/design-screenshots.mjs` takes full-page screenshots of home, events, newsletter, shows archive, shop and donate at 1280, 901, 375 and 320 pixels, but it accepts only a plain loopback HTTP origin, so it cannot test the real site.
    - GAP: no documented accessibility audit, link-check or performance measurement procedure for the deployed site. See G-38.
    - Worked if: each gate group has a dated, recorded result.
- [ ] **8.2 Cutover gates that are not code** (from website repo `docs/wordpress-parity-plan.md` and `docs/migration.md`). Confirm every one:
    - Who: You
    - Status: NOT YET BUILT
    - [ ] editorial review of imported pages done (4.8)
    - [ ] integration sign-off: forms, donations in provider sandbox, merchandise link, calendar and Drive (4.12, 4.13, 8.9)
    - [ ] backup and restore passed, including an isolated restore of the WordPress ZIP (3.7, 7.7)
    - [ ] redirects tested, including Kinsta-held rules (4.10, G-1)
    - [ ] final content delta reconciled (3.10, 4.14)
    - [ ] production identity: PostgreSQL, TLS, restricted administration, tested restore (Part 5, 7)
    - Worked if: all boxes ticked.
- [ ] **8.3 Rehearse the rollback.** Pieces that exist: keep WordPress untouched and live until parity passes; switch the website to the previous immutable release root; restore a verified database backup with the matching uploads (not a migration downgrade).
    - Who: Claude can do this for you
    - Status: NOT YET BUILT
    - GAP: no rollback rehearsal for the cutover as a whole, and no DNS revert procedure (G-31). `docs/website-host.md` states schema downgrade compatibility is not proven.
    - Worked if: you have a written, time-boxed rollback you have rehearsed once on the rehearsal environment.
- [ ] **8.4 Final content freeze and last export, then final apply.**
    - Who: You (freeze), Claude (export and apply)
    - Status: NOT YET BUILT (G-4, G-5)
    - Worked if: the last export was applied as drafts, reviewed, published, and redirects re-activated (4.9, 4.10).
- [ ] **8.5 Lower DNS TTL and record the current DNS.**
    - Who: You
    - Status: NOT YET BUILT
    - GAP: no documented procedure. The parity plan only says to capture "DNS records/TTL" and not to assume DNS is in a site snapshot. Needed: which provider holds DNS for `princetonterraceclub.org` and `terracefclub.org`, the records to change, the TTL plan and an exact order of changes. See G-31.
    - Worked if: you have a saved copy of every current record.
- [ ] **8.6 Change DNS to the new origin.**
    - Who: You
    - Status: NOT YET BUILT (G-31)
    - GAP: no documented procedure and no DNS change has been made. The gateway needs the exact hostnames first (1.19) and a real certificate (G-12).
    - Worked if: the new hostname resolves to the production host and serves a trusted certificate.
- [ ] **8.7 Verify after the switch.** Checks that exist and what they cover:
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (gateway protocol suite on a disposable stack, synthetic CA)
    - What to confirm on the real origin: certificate and hostname trust; spoofed forwarding ignored; SNI/Host mismatch gives 421; identity admin and master-realm paths not reachable; cache headers on private and error routes are `private, no-store`; direct backend access denied; member and editor sign-in; `/apply/` opens and the rate limit sees real client addresses; old URLs redirect; drafts return 404; published pages and media load.
    - GAP: no external-probe procedure against the production origin is written; the existing suite runs inside disposable guests. See G-23, G-38.
    - Worked if: each check passes from outside the host, and the old URLs still resolve.
- [ ] **8.8 Retire the old servers.** Only after verified parity: stop new writes to the old systems, then decide on WordPress and the old Hetzner server. Never resize the old Hetzner server; deletion is a separate decision of yours.
    - Who: Needs your decision
    - Status: NOT YET BUILT
    - GAP: `docs/migration.md` says to "prevent the old server from continuing writes"; no procedure or ordering is written. The rehearsal `retire-original` step is for rehearsal servers only (not reached in leases 3 and 4). See G-3.
    - Worked if: the old server's final data copy is verified restorable before any deletion.
- [ ] **8.9 Turn on live donations.** Only after test Payment Links, receipts, cancellation, failed and abandoned payment checks and reconciliation pass in Stripe's test mode. Set `DONATIONS_MODE=live` and `DONATION_CHECKOUTS_JSON` with live `https://buy.stripe.com/<link>` URLs (live mode rejects `/test_` links). Keep the old provider for existing recurring gifts.
    - Who: You (Stripe), Claude (configuration)
    - Status: TESTED IN CODE ONLY (validation and the HTTP boundary with synthetic destinations; no Stripe transaction has occurred)
    - Worked if: a visitor on the donate page reaches the right hosted link for the chosen fund and frequency, and the displayed amount, recipient and purpose match. Never perform a live charge as a test.
- [ ] **8.10 Switch the membership workflow to the new platform.** Pause old roster writes, reconcile historical rows (they have no markers), rehearse a duplicate and a lost acknowledgement on a disposable spreadsheet, verify the webhook route, then switch.
    - Who: Needs your decision
    - Status: NOT YET BUILT
    - GAP: see 6.19 and G-26. The payment email, its own delivery ledger and the flow switch are separate unbuilt work.
    - Worked if: a completed synthetic membership produces exactly one roster row and one message.
- [ ] **8.11 CDN last.** Only after origin correctness, private/admin/member bypass tests and cache exclusion tests.
    - Who: Needs your decision, then Claude
    - Status: NOT YET BUILT
    - GAP: policy and plan only (Womb issue 14). The gateway currently sends `private, no-store` for every response, so nothing is cached. Needed: a provider choice and an actual-provider proof of edge cache-key behaviour, cookie/authorization/query bypass, edge rules, purge and withdrawal timing, stale and error serving, origin isolation, TLS termination and external access. Planned CDN allowance is $0, subject to provider terms. See G-32.
    - Worked if: unpublishing a page removes it from the edge within a measured time and member and admin routes are never cached.

---

## Part 9. Day-two management

Nothing below runs against production yet because no production host exists. These are the routines to adopt, with how to ask Claude. Until a production host exists, "ask Claude" means: ask for the check against the rehearsal environment, or ask Claude to prepare the procedure.

### Weekly

- [ ] **9.1 Backup freshness.** On each host run `terrace-backup age` and `systemctl --failed`. Ask Claude: "Check backup age on every Terrace host and list any failed units."
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (`docs/backups.md`)
    - Worked if: exit 0 and no failed backup, check or freshness unit. Also look at the weekly `terrace-backup-check` result. There is no alert delivery (G-23).
- [ ] **9.2 Service health.** `systemctl start terrace-identity-health.service` and read `journalctl -u terrace-identity-health.service --since '-15 min' --no-pager` (`docs/identity-host.md`). Ask Claude: "Run the identity health check and report."
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL
    - Worked if: the health unit succeeds. The unit emits only a fixed failure message.
- [ ] **9.3 Application queue and moderation queue.** In the CMS admin open **Member applications** and **Moderation reports**. Pending applications are capped at 500 and withdrawn after 60 days; a staff notice is added past 400.
    - Who: You
    - Status: PROVEN LOCALLY (real-browser report check; applications in the handoff, real-export local rehearsal 2026-11-18)
    - Worked if: nothing waits longer than the policy in 1.7 and 1.8.
- [ ] **9.4 Open pull requests and CI.** Ask Claude: "Summarise open Terrace pull requests and the last CI and mirror runs for womb and tfc-website." GitHub is primary; GitLab is an archive and one-way mirror.
    - Who: Claude can do this for you
    - Status: PROVEN LOCALLY (read-only `gh` checks; CI and mirror evidence is recorded in `docs/github-migration.md` in both repos)
    - Worked if: the last runs on `trunk` and `dev` are green and the mirror matches.
- [ ] **9.5 Cost.** Compare the Hetzner invoice with the US$80 ceiling, including tax, backups and any overlap. Ask Claude to read the project inventory (read-only).
    - Who: You
    - Status: NOT YET BUILT
    - GAP: only the quote guard in `infra/cloud` exists; no invoice reconciliation procedure or alert. See G-9.
    - Worked if: the actual month is strictly below $80.

### Monthly

- [ ] **9.6 Restore drill** on a replacement host or the rehearsal VM (7.7). Ask Claude: "Run a restore drill from the latest primary and secondary snapshots and report the times."
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL
    - Worked if: `RECONCILIATION OK` for both targets, with times recorded.
- [ ] **9.7 Updates.** Pins to review: Nix inputs (`platform/flake.lock`, nixpkgs `31ecbaca`), Keycloak 26.8.1 (client and realm changes need reviewed reconciliation, not a re-import), Windmill 1.861.0, Documenso v2.21.1, Vaultwarden 1.37.5, Node 24.21.0 and Bun 1.4.4 (website bundle runtime), Payload and Next. The CMS printed a notice that Next 16.4.3 has a known vulnerability, which needs a deliberate dependency update (the handoff queues a Next.js security update on the website side). Inspect a candidate image with `direnv exec . bun platform/oci-image.ts windmill-labs/windmill 1.861.0` (`docs/windmill-host.md`; `docs/oci-images.md` shows the same form for `1.839.0`). Ask Claude: "Review upstream security advisories and propose pin updates with rehearsal evidence."
    - Who: Claude can do this for you
    - Status: TESTED IN CODE ONLY
    - GAP: there is no automatic promotion or rollback pipeline across services, and a pin change must re-run the matching VM check. See G-37.
    - Worked if: each update has a passing VM or release check and a rollback path before it is deployed.
- [ ] **9.8 Capacity.** Memory, disk and PostgreSQL connection headroom against the 25 percent rule; backup repository size (`terrace_backup_repository_bytes`) against the storage allowance (about $5 per month).
    - Who: Claude can do this for you
    - Status: NOT YET BUILT (G-24, G-23)
    - Worked if: no out-of-memory events and storage growth is inside the allowance.
- [ ] **9.9 Credential rotation.** Rotate only with consumers mapped and a recoverable backup. Documented mechanics: replacing the identity database password file does not rotate an existing database role; TLS file replacement needs an nginx restart; Windmill, Documenso and Vaultwarden need the stopped-writer sequence; the Windmill encryption key must not change before Windmill state is backed up and recovery verified; Documenso encryption keys must stay with recovery inputs; GitHub mirror deploy key rotation is documented (`docs/github-migration.md`: create a new deploy key, grant only that key protected-branch write, replace the Actions secret, verify a mirror run, then revoke the old key). Ask Claude: "Plan the rotation of <credential>, listing every consumer and the backup state first."
    - Who: Claude can do this for you; You approve
    - Status: NOT YET BUILT
    - GAP: no rotation runbook for Keycloak client secrets, database passwords, restic passwords or Hetzner tokens. Legacy credentials that leaked in old source need consumer mapping first (G-28). See G-37.
    - Worked if: old credentials are rejected and every consumer still works.
- [ ] **9.10 Redbubble catalogue.** The six curated product links must be re-checked periodically; there is no automatic sync.
    - Who: You
    - Status: PROVEN LOCALLY (`web/docs/merchandise.md`, checked 2026-10-28)
    - Worked if: each link still opens the right product.
- [ ] **9.11 Access review.** List administrators, editors and approved Windmill, Documenso and Vaultwarden users against your current named lists. Remove people who left (the stopped-writer bootstrap for Documenso, Windmill and Vaultwarden, and role removal in Keycloak).
    - Who: You
    - Status: NOT YET BUILT
    - GAP: no listing tool for Keycloak users and roles in production (G-15).
    - Worked if: every account maps to a current, named person.
- [ ] **9.12 Retention sweep.** Once retention is decided (1.10) run `lifecycle:sweep` on a schedule (5.17). Ask Claude: "Run the lifecycle sweep and report what expired."
    - Who: Claude can do this for you
    - Status: NOT YET BUILT (production; G-18)
    - Worked if: the sweep report shows no pending mirror redactions.

### Per event

- [ ] **9.13 Someone leaves the club.** Offboard (5.13), then close the realm side. Documenso, Windmill and Vaultwarden need their own approval lists updated and the bootstrap rerun with writers stopped. Ask Claude: "Offboard <person> everywhere and list what remains."
    - Who: You; Claude helps
    - Status: PROVEN LOCALLY for the website member part (handoff, real-export local rehearsal 2026-11-18); NOT YET BUILT for production
    - Worked if: the member cannot sign in anywhere and their content is hidden.
- [ ] **9.14 A member asks for erasure.** Follow 5.16, then the sweep (5.17).
    - Who: You
    - Status: PROVEN LOCALLY for the website side (handoff, real-export local rehearsal 2026-11-18); sweep part TESTED IN CODE ONLY
    - Worked if: the sweep reports no pending redactions and the confirmation was written once.
- [ ] **9.15 A report arrives or a member complains.** Moderation owner follows 5.14; the appeal process is undefined until you set one (1.8).
    - Who: You
    - Status: PROVEN LOCALLY
    - Worked if: a recorded decision exists.
- [ ] **9.16 New editor or new administrator.** Grant the role in Keycloak (5.6). Ask Claude for a role-access check once a production console procedure exists.
    - Who: You
    - Status: NOT YET BUILT (G-15)
    - Worked if: the editor sees only editorial areas.
- [ ] **9.17 Publish a new website release.** Build, verify, smoke, re-pin Womb, run the website and gateway checks, back up, migrate and start (6.12 to 6.15). Ask Claude: "Prepare release <revision> through the checks and tell me when it is ready to deploy."
    - Who: Claude can do this for you
    - Status: NOT YET BUILT for the current revision (G-13)
    - Worked if: the previous release is still on disk for rollback.
- [ ] **9.18 After any incident or restore.** Preserve evidence, never delete a failed run, restore only into isolation first, prevent the old host from writing, then switch traffic. Ask Claude: "Restore Terrace from backup into isolation and compare it with the last good counts."
    - Who: Claude can do this for you
    - Status: PROVEN IN VM REHEARSAL (synthetic)
    - Worked if: reconciliation passes before traffic moves.
- [ ] **9.19 A Hetzner lease for testing.** Never idle compute. Say the exact yes ("Yes, run lease N") only after Claude states the budget (the internal planning cap is under $5 per run) and the stop conditions. Confirm afterwards: zero servers, IPs, keys or firewalls from the run and the baseline unchanged.
    - Who: Needs your decision
    - Status: PROVEN ON HETZNER (every probe and lease 1 to 4 tore down with three absence reads; no lease has completed the services, backup and restore stages)
    - Worked if: Claude's teardown report shows absence in three reads.

---

## Part 10. Moving on-prem later

Nothing is built for the physical server. `docs/revival.md` says not to invent a disk layout, and the same `single-node.nix` module is the intended base with a separate host profile. Single-node downtime is acceptable and nothing spans the WAN: no quorum or storage replication across sites, and a CDN or backups do not create HA.

- [ ] **10.1 Collect the hardware inventory.**
    - Who: You
    - Status: NOT YET BUILT (an input)
    - Collect (from `docs/revival.md`, `docs/manual-steps.md`, `docs/migration.md`): CPU and RAM; every disk (model, size, serial, `/dev/disk/by-id` path) and which is the system disk; boot mode (UEFI or BIOS) and whether you can get Console or IPMI access; network interfaces, addresses, router and firewall control, whether you can open UDP and TCP ports; the Frigate cameras (make, model, streams, how many), the accelerator (Coral or GPU), storage and retention wanted for recordings; Home Assistant devices, radios and LAN access; power and UPS arrangements if any (not in the docs, but needed for planning).
    - Worked if: a private list exists; do not paste serials into chat.
- [ ] **10.2 Write the on-prem host profile and disk layout.**
    - Who: Claude can do this for you, after 10.1
    - Status: NOT YET BUILT
    - GAP: no procedure. The Hetzner disk profile (UEFI, GPT, ext4, Disko) is cloud-specific; on-prem needs its own verified disk path and install steps. See G-33.
    - Worked if: the profile evaluates and installs on the real disks in a rehearsal.
- [ ] **10.3 Decide what carries over from the cloud host.**
    - Who: Needs your decision
    - Status: NOT YET BUILT
    - Carries over (declarative): the Git repository and Nix modules; the pinned images and release bundles; Keycloak realm and database (restore from backup); website and CMS database and uploads; Headscale database and its Noise and DERP keys (restore the keys before first start so clients keep their identity); the restic repositories and escrowed passwords; Documenso, Windmill and Vaultwarden data and their input folders.
    - New: the physical machine and disk layout; the on-prem install procedure; real Home Assistant and Frigate with real devices; per-user SSO for them; Headscale grants that give the application host a specific path to the on-prem devices (today deny-all); rolling NVR footage is excluded from backups while important clips and exports are retained separately; a new decision on whether the cloud relay stays as the independent access host.
    - GAP: no documented migration procedure from the cloud host to on-prem. `docs/migration.md` gives only the principle: reprovision from the repository, restore, validate, switch traffic, and prevent the old server from continuing to write. See G-33.
    - Worked if: you can say, per service, "moves" or "stays" and where its data restores from.
- [ ] **10.4 Rehearse the move.** Restore a copy of production into the on-prem host in isolation, with email, payment and signing disabled, before any traffic moves.
    - Who: Claude can do this for you
    - Status: NOT YET BUILT (G-33)
    - Worked if: counts, hashes, SSO logins and the Headscale client reconnection check all pass with the cloud host still serving.
- [ ] **10.5 Home Assistant and Frigate real integration.**
    - Who: Claude can do this for you, after 10.1
    - Status: NOT YET BUILT
    - GAP: only synthetic demos exist (Womb issues 9 and 10). Real cameras, accelerator, storage and retention, individual Frigate accounts and real HA devices are all unwritten. The Frigate demo module requires a dedicated nginx and rejects other host vhosts.
    - Worked if: a real camera stream and one real sensor appear for a signed-in approved person only.

---

## Open gaps

Each row is a GAP (no documented procedure), a NOT YET BUILT item, or a conflict between documents. "Closes it" says what would finish it.

| ID | Gap | Kind | What would close it |
| --- | --- | --- | --- |
| G-1 | Export of Kinsta redirects, Nginx and IP-deny rules, DNS, cron, access restrictions (3.4) | GAP | A written MyKinsta and DNS checklist, a private ledger, and a converter into the importer's redirect format |
| G-2 | Plugin and integration inventory; isolated restore and comparison of the Kinsta ZIP (3.5, 3.7) | GAP | An isolated WordPress runtime, a restore recipe, an allowlisted reader for plugin options in the SQL, a ledger template |
| G-3 | Inventory and protected copy of the existing Hetzner server; retiring it (3.9, 8.8) | GAP | Access recovery, an inventory checklist, a verified restore, then your delete decision |
| G-4 | Content freeze and final delta strategy (3.10, 8.4) | GAP | A dated freeze plan using the importer's repeat-by-ID behaviour |
| G-5 | Running the importer against the production database (4.6) | GAP | A packaged importer or a documented operator run on the host, and a reviewed use of `--allow-nonlocal-database` |
| G-6 | Media bytes and newsletter PDFs from the ZIP (4.7) | NOT YET BUILT | The ZIP, the `MediaBytesProvider` reader, hash reconciliation |
| G-7 | Menus (11 items), plugin options and Kinsta redirect rules from the SQL (4.7) | NOT YET BUILT | `DatabaseTableProvider` implementations and a navigation collection |
| G-8 | Draft preview for any collection (none exists; needs an owner decision on editor authentication to the website); visual review of 24 `divi-layout-review` items (4.8) | NOT YET BUILT | A draft preview route or a staging site |
| G-9 | Production sizing and creation path conflict: option A (CX23 and CX43, Pulumi, Debian bootstrap) versus rehearsal (CPX22 and CPX42, Rescue RAM installer); public pages showed CX, CAX and CPX as unavailable on 2026-10-31 yet CPX servers were created in rehearsals; CX stock never checked with the project token; no invoice reconciliation (1.15, 6.9, 9.5) | GAP and CONFLICT | Your sizing decision, an authenticated stock and price check, one written production creation procedure |
| G-10 | Production host profiles enabling the modules (6.10) | NOT YET BUILT | Real profiles that build and boot with real options |
| G-11 | Production secret delivery, generation and escrow retrieval (5.3, 6.11, 7.4) | NOT YET BUILT | sops-nix or agenix (or equivalent) adopted, tested delivery, an escrow retrieval drill |
| G-12 | Certificate issuance, renewal and HTTP-to-HTTPS redirect for the gateway (6.16) | NOT YET BUILT | A chosen method (ACME or manual), port 80 redirect, expiry alerting |
| G-13 | Website bundle for `dad0a20` (nine migrations, Next.js 16.4.5); Womb pins `3902e16` (`platform/website-release.json`, six-migration ledger); hash-pinned producer files and the full rehearsal use old bundles; Womb has a dated note about this and the re-pin is queued work (6.12, 6.14) | NOT YET BUILT | Build the bundle, update `website-release.json` and pins, re-run website, gateway and full checks |
| G-14 | Production environment for the website and CMS is not wired in Womb: `CMS_PUBLIC_ORIGIN`, `LIFECYCLE_*` and `ADDRESS_HEADER` were documented in three places and the website now has one production configuration table (website repo `docs/member-lifecycle.md`, PR 11), but Womb's gateway code overwrites `X-Forwarded-For` (one hop) with no Womb document saying so and no Womb module sets any of these variables (5.7) | GAP | The variables wired into the Womb website module and a test that `ADDRESS_HEADER` and `XFF_DEPTH` match the gateway |
| G-15 | Production Keycloak admin access, creating admin and editor accounts, granting roles, listing users (5.5, 5.6, 9.11, 9.16) | GAP | A written procedure (private path to the console, bootstrap admin removal, role grants, audit) |
| G-16 | Production member provisioning and offboarding; verified email without mail; temporary password delivery (5.11, 5.13) | GAP | A console procedure or a production-safe provisioning tool, and a delivery method |
| G-17 | No way to bulk onboard a roster; applications can only be created by `/apply/`, capped at 500 pending (5.18) | NOT YET BUILT | A decision plus, if wanted, a bulk invitation tool |
| G-18 | Sweep and importer are not in the release bundle (only `bin/start` and `bin/migrate`); the sweep has a documented source-checkout invocation and a recommended daily schedule (PR 11) but no packaged `bin/sweep` and no timer; the importer has no production invocation (5.17, 9.12, 4.6) | GAP | A packaged `bin/sweep`, the Womb timer and service account, and a production importer run |
| G-19 | Production outbox folder: location, service write access, backup inclusion, operator read path (5.10); the website now requires an absolute directory outside code and media and the same account for CMS and sweep, and refuses to start a public CMS without it | GAP | Define the path, sandbox permission, `extraPaths` entry and a read procedure |
| G-20 | No mail transport anywhere (invitations, Keycloak, Documenso, payments email) (1.12) | NOT YET BUILT | Your delivery choice, then an implementation and sandbox tests |
| G-21 | Real Storage Box and B2 never tested; scheduled backup is not quiescent; K3s datastore and PV data not covered; provider image restore undocumented (7.1, 7.9, 7.10, 7.11) | GAP | A real-target drill, a quiesce procedure, K3s and PV coverage or exclusion, provider-image procedure or drop |
| G-22 | Procedure to promote a restore into service (7.8) | GAP | A written, drilled, owner-approved production procedure |
| G-23 | Alert delivery, external probes, certificate expiry, log retention and redaction (6.23, 7.6, 8.7) | NOT YET BUILT | A monitoring and alert design and a tested alert you receive |
| G-24 | Combined-stack capacity measurement (6.22, 9.8) | NOT YET BUILT | A week-long measured run with at least 25 percent headroom |
| G-25 | Documenso signing mail delivery and production signing certificate (6.18) | NOT YET BUILT | Mail transport (G-20) and a certificate decision |
| G-26 | Membership workflow cutover: webhook mapping and authentication, legacy sheet rows, QuickBooks email, delivery ledger, Windmill ten-SSO-user limit (6.19, 8.10) | NOT YET BUILT | Your operator access (1.17), a sandbox rehearsal, reconciliation of legacy rows |
| G-27 | Existing Vaultwarden instance inspection and data migration (6.20) | GAP | Inspect the live instance, a protected copy and a migration test |
| G-28 | Legacy credential remediation: map consumers, back up, rotate, prove old ones rejected (1.17, 9.9) | NOT YET BUILT | Operator access and a consumer map (Womb issue 12) |
| G-29 | Replacement for Gravity Forms forms (4.12) | NOT YET BUILT | Your keep or drop decisions, then submission collections with spam and upload controls |
| G-30 | Source of truth for events, Google Calendar sync and Drive menu documents (4.13) | NOT YET BUILT | A decision and an import or replacement |
| G-31 | DNS TTL, DNS change order, DNS revert (8.5, 8.6, 8.3) | GAP | Provider access, the record list and an exact, rehearsed order |
| G-32 | CDN (8.11) | NOT YET BUILT | Provider choice and an actual-provider proof of the cache and bypass rules |
| G-33 | On-prem host profile, cloud-to-on-prem migration and rehearsal (10.2 to 10.4) | NOT YET BUILT | Hardware inventory, a verified disk layout, a written migration |
| G-34 | Headscale human OIDC enrollment, WAN and forced DERP, reviewed grants (6.17) | NOT YET BUILT | Native real-client proof across a WAN and a reviewed policy |
| G-35 | Stripe sandbox and live setup (1.3, 8.9) | NOT YET BUILT | The account, test Payment Links and reconciliation |
| G-36 | Document conflicts (see list below this table). Items 1, 2, 4, 5, 7, 8 and 10 are fixed in Womb by the docs PR that carries this file; items 6, 9 and 12 and the website release README are fixed on website `dev` `dad0a20`; only item 3 (a workspace file) remains | CONFLICT | Rewrite the handoff's old lease 4 paragraph |
| G-37 | Updates promotion, rollback and rotation runbooks across services (9.7, 9.9) | NOT YET BUILT | A written pipeline and per-credential rotation runbooks |
| G-38 | Deployed-site accessibility, link, performance and external-origin verification procedures (8.1, 8.7) | GAP | A documented audit and measurement procedure against the real origin |
| G-39 | Reconciling client or realm changes in an existing Keycloak realm (the import is create-only) (5.4) | NOT YET BUILT | A tested authenticated reconciliation procedure |
| G-40 | Independent emergency access to the identity provider: `docs/revival.md` lists it as remaining evidence and `AGENTS.md` says recovery access must not depend on SSO, but nothing documents or tests a break-glass path for Keycloak, Headscale or the hosts (5.5, 5.6) | GAP | A written, tested emergency-access design that does not need Terrace SSO |
| G-41 | Open defects from the real-export local rehearsal (handoff, real-export local rehearsal 2026-11-18), being fixed on the website side: the admin screens cannot link a member to an application or create an offboarding (REST only); erasure leaves personal data when the application was never linked; nine near-empty pages and one raw `[tt-event]` shortcode were publish-eligible; publishing in the admin does not activate a redirect until `apply` is re-run and `apply --publish` publishes nothing on an already-imported set; four internal links 404 and 27 `/wp-content/` links still point at the old site; one dead `/join` redirect row (2.14, 4.8, 4.9, 4.10, 5.12, 5.13, 5.16). Fixed on `dev` `dad0a20`: the `/apply/` class-year field and the unstyled apply and invitation pages | GAP (defects) | The website fix workers' merges, then a re-run of the real-export rehearsal |
| G-42 | Lease reliability depends on a shared builder: lease 4 stopped at the plan step because a non-Terrace job took Falcon's shared build lock and the planner's 100 second wait expired (6.6). A fix is in progress offline: prebuild before arming, fair-queue lock waits and a time ledger | GAP | The tooling fix merged and proven offline, then a fifth lease with your yes |

### Document conflicts and stale statements (G-36)

1. `docs/backlog.md` (2026-11-05), `docs/revival.md` and `docs/manual-steps.md` said dedicated Hetzner credentials are unavailable and no provider apply was run; the handoff records an authorised token on 2026-11-07 and disposable probes and leases 1 to 4 since (lease 4 stopped before the services). Fixed in Womb by the docs PR carrying this file.
2. `docs/community-pilot.md` (proposed, 2026-10-28) recommends HumHub with MariaDB; the accepted decision is the Payload and SvelteKit pilot. `docs/backups.md` still named a "community" database and moderator ownership by reference to it. Fixed in Womb (superseded banner and a backups note); `platform/backup-test.nix` and the `backup.nix` option example still use the name `community` for a synthetic database.
3. The handoff's older paragraph "Lease 4 (not started) ... ready to arm" is superseded by the newer lease 4 checkpoint (stopped at the plan step), but still reads as current. Workspace file: not fixable in Womb.
4. Womb `docs/website-host.md` and `docs/website-gateway.md` describe bundles at `3902e16` and a six-migration ledger; tfc-website dev now has nine ledger rows, derived from the migration registry, and `tools/release/README.md` no longer states a count. Womb side: dated notes added (pins and hashes unchanged); the re-pin is tracked work.
5. `docs/access-relay.md` and `infra/cloud` default to CX23 and CX43; the rehearsals ran CPX22 and CPX42. Womb side: discrepancy documented in `access-relay.md`, production sizing marked as an owner decision, code defaults unchanged.
6. Dry-run counts: the handoff said 86 redirects and two gaps; website repo `docs/wordpress-import.md` said 87 and one. Fixed on website `dev` `dad0a20` (86, 80 plus 6, two gaps, the final dry run).
7. `docs/identity-host.md` says the proxy denies admin and there is no remote admin route; website repo `docs/member-lifecycle.md` tells a production operator to use the Keycloak admin console. Womb side: kept, with a marked GAP added (G-15).
8. `docs/service-lifecycle.md` (2026-11-04) listed Documenso, Windmill and Vaultwarden as having no new-platform proof; later docs and the all-services rehearsal prove them in VMs. `docs/backups.md` said they are "not deployed yet". Fixed in Womb: "proven in the VM rehearsal, not deployed to production".
9. Local ports: `identity/README.md` used 8184, website repo `docs/local-preview.md` 8186, and the lifecycle doc examples 8190. Fixed on `dev` `dad0a20`: the docs explain that 8184 is the standalone identity default, 8186 the `local-stack` default, and the lifecycle examples use 8186.
10. Many Womb docs link to GitLab issues and merge requests; GitHub is primary. Fixed in Womb where a GitHub equivalent exists (issues; website blob links); remaining GitLab links are labelled historical. GitLab `!N` merge-request numbers do not map to GitHub PR numbers except the four listed in `docs/github-migration.md`.
11. Recovery targets: `docs/backups.md` says at most 24 hours of data loss and four hours to recover (taken from the superseded community pilot); `docs/migration.md` proposes a 24 hour RPO and same-day recovery with an operator. Both are unmeasured. Found in verification; noted in Womb `backups.md`, not reconciled.
12. Website repo `docs/wordpress-import.md` said `cms/tools/ci.ts` "now expects seven ledger rows" and pointed to `tools/local-up`. Found in the first verification pass; fixed on `dev` `dad0a20`.

---

## Decisions waiting on you

| # | Decision | Default currently in code or docs |
| --- | --- | --- |
| D-1 | Approvers, fallback approvers, whether editors may approve, approval wording and evidence | Editors and administrators can both decide; wording is a placeholder |
| D-2 | Moderation owner, backup, response targets, appeal process | None |
| D-3 | Invitation lifetime | 7 days (`LIFECYCLE_INVITATION_DAYS`, `cms/src/lifecycle/policy.ts`) |
| D-4 | Offboarding and moderation grace period | 30 days (`LIFECYCLE_GRACE_DAYS`, `policy.ts`) |
| D-5 | Pending application age-out | 60 days (`LIFECYCLE_APPLICATION_DAYS`, `policy.ts`) |
| D-6 | Retention for moderation content, reports, tombstones, outbox, audit rows | Nothing deleted automatically; no scheduled sweep (recommended: daily, without `--purge-moderated`, until you decide) |
| D-7 | Erased founder's group name and description | Kept verbatim |
| D-8 | How invitations and notices are delivered; Keycloak first-password handling; mail transport | Outbox file copied by a person; no transport |
| D-9 | Whether alumni and current members need different approval evidence | Same process |
| D-10 | How the real roster becomes accounts (everyone applies versus bulk invite) | Everyone applies at `/apply/` |
| D-11 | Primary and backup identity administrator; editor list; Keycloak retention | None named |
| D-12 | Hosting sizing, region, billing country and VAT; production creation path | Option A in code: `infra/cloud/budget.ts` defaults `cx23` in `hel1` and `cx43` in `nbg1`, provider backups on both, about $37.18 steady per `docs/access-relay.md`; the guard rejects an overlap month reaching `MONTHLY_CEILING_CENTS = 8000`. The rehearsals ran CPX22 and CPX42 in `nbg1`; no code default changed |
| D-13 | What happens to the existing Hetzner server (keep, copy, retire) | Left untouched; never resized |
| D-14 | Each Hetzner lease | Needs a fresh explicit yes; internal planning caps of $5 per run and $20 per month are in `docs/service-lifecycle.md` prose, not enforced in code (`infra/cloud/README.md`: the guard is a quote guard, not a billing cap) |
| D-15 | Gravity Forms: keep or drop each of the seven forms | Entries excluded; nothing rebuilt |
| D-16 | Event and menu source of truth; Google Calendar and Drive | Sample events labelled fictional; official events page linked |
| D-17 | Donations: Stripe account, links, wording; live switch | Unset `DONATIONS_MODE` behaves as `disabled` (`web/src/lib/server/donations.ts`); old donation page remains; no tax-deductibility claim |
| D-18 | Backups: Storage Box and B2, dataset owners, escrow location, provider image backups, quiet window | Two targets in the plan, none exist; owners "TBD"; retention 3 plus 7 daily, 4 weekly, 6 monthly |
| D-19 | Public hostnames and certificate method | `headscale.terracefclub.org` and `vaultwarden.terracefclub.org` only; port 80 closed |
| D-20 | CDN provider and timing | None; after origin acceptance; all responses currently `private, no-store` |
| D-21 | Content freeze and cutover date | None |
| D-22 | When to retire WordPress | Keep until parity and rollback pass |
| D-23 | Membership workflow switch and Windmill, Documenso, Vaultwarden access | Old workflow preserved; roster script opt-in and untriggered |
| D-24 | On-prem hardware, disks, network, cameras, accelerator | Unknown; no layout invented |
| D-25 | Whether the website session store (in memory, one process, eight-hour sessions) is acceptable for launch | In-memory, 8 hours (`web/src/lib/server/member-auth.ts`); restart signs everyone out; no replicas |

---

## Sources read

- The private handoff file (top two resume checkpoints, Kinsta intake, Hetzner sections, owner intent, external blockers).
- Womb `origin/trunk` `01464be`: `README.md`, `docs/*` (backlog, manual-steps, revival, migration, membership-roster, community-pilot, backups, access-relay, identity-host, identity-recovery, website-host, website-gateway, service-lifecycle, demos, windmill-host, documenso-host, documenso-mail-evidence, vaultwarden-host, cloud-rehearsal, cloud-rescue-install (first part), full-rehearsal, github-migration, oci-images, postgresql-bootstrap-restore (first part)), `platform/README.md`, `platform/single-node.nix`, `platform/hetzner.nix`, `platform/backup.nix` (options), `infra/cloud/README.md`, `infra/nixos/README.md`. `docs/restore-disk-evidence.md` was not read. `AGENTS.md` was read during verification.
- tfc-website `origin/dev` `8883875`, re-read at `dad0a20` for the changed files (website repo `docs/member-lifecycle.md`, `docs/wordpress-import.md`, `docs/local-preview.md`, `identity/README.md`, `tools/release/README.md`, `web/README.md`, `cms/src/runtime-policy.ts`, `cms/src/lifecycle/outbox.ts`): website repo `docs/wordpress-import.md`, website repo `docs/member-lifecycle.md`, website repo `docs/wordpress-parity-plan.md`, website repo `docs/local-preview.md`, `tools/release/README.md`, `identity/README.md`, `web/README.md`, `web/docs/{community,donations,merchandise}.md`, `cms/README.md`, `cms/COMMUNITY.md`, the two `package.json` scripts, and the release and import tool headers.
- No credential file, state file or anything in the owner's private folders was read; those are referred to only generically.

---

## Verification log

Verified 2026-11-18 by an independent documentation worker against Womb `origin/trunk` `01464be`, tfc-website `origin/dev` `8883875`, `CLAUDE_HANDOFF.md` and `WORKER_RULES.md`; second pass below. Method: every fenced command line and inline command was matched, whitespace-normalised, against the text of the cited repository documents, READMEs, the handoff and the workspace rules; every backticked path was tested for existence in the two source trees; every status label and every default in the gaps and decisions tables was checked against the evidence or grepped in the code.

| Check | Result |
| --- | --- |
| Fenced command lines in the draft (35 blocks, a nix block counted as one) | 72 |
| Verbatim in a source | 67 |
| Adapted by design and flagged in the step (placeholders, a new report file name, or assembled from documented flags: 2.10 `install -d` and apply, 4.10 apply, the two 6.14 checks) | 5 |
| Transcription errors found in fenced commands | 0 |
| Fenced commands changed in this pass | 2 (2.13, 2.14 and 5.13: port 8190 became the default 8186; the source document now uses 8186 too) |
| Fenced commands added | 4 (2.2 worktree commands from `WORKER_RULES.md`, and a `cd ..` in 2.10) |
| Inline commands (draft) | 11, of which 3 removed (2.1: `nix --version`, `devenv version`, `direnv version` exist in no source; step now says GAP: no documented procedure) and 1 annotated (6.2 `cloud-controller.py binding`: handoff only, repository tool named) |
| Backticked repository paths checked | 101 distinct; all repository paths exist. Not checkable: private or runtime paths (the owner's private folders including the dry-run report, the builder's work folders, `identity/.state/...`, `/var/lib/...`) |

Status labels changed (12): 2.1 PROVEN LOCALLY to NOT YET BUILT (no documented check); 2.13, 2.14, 5.8 (local part), 5.9, 5.10, 5.12, 5.13, 5.17, 9.13 and 4.11 PROVEN LOCALLY to TESTED IN CODE ONLY (the evidence is tests, some against a fake Keycloak API, with no recorded live run); 9.3 split (moderation reports stay PROVEN LOCALLY, applications become TESTED IN CODE ONLY). Clarified without changing the label: 4.2, 6.2, 6.13, 6.16, 9.4, 9.19, and the legend entries for PROVEN LOCALLY, TESTED IN CODE ONLY and IN PROGRESS (lease 4 is pre-arm per the handoff). Hetzner labels (6.1, 6.17) were checked against the lease 3 record and kept.

Gaps: 0 removed (each G-1 to G-39 is still undocumented in the sources); 2 added (G-40 emergency identity access, G-41 flows never verified in a browser); 4 reworded (G-9, G-13, G-14, G-36). Conflicts added: 2 (items 11 and 12). Decisions: 25 checked; D-3, D-4, D-5, D-12, D-14, D-17 and D-25 now cite the code location of their default and D-12, D-14 were corrected (the guard constant, and that the $5 and $20 caps are prose, not enforced).

Other corrections: 6.4 pointed at 6.5 instead of 6.7; 1.15, 6.9 and G-9 said CX was unavailable, but the same pages showed CPX unavailable while CPX servers were created; the 16 MiB journal claim in 6.23 was 8 MiB for the website module; the `$5` rehearsal cap source; Part 2 rewritten for literal use (where to type, short checkout path with the computed 61-byte limit, private window for the member sign-in, report files must be new).

Not verified: contents of the private dry-run report and anything in the owner's private folders; Kinsta, MyKinsta and provider screens; the claims quoted from the handoff about Falcon receipts; the Documenso, Windmill and Vaultwarden "Worked if" outcomes beyond what their host documents state; whether the Mac has Nix, devenv and direnv (2.1); no step of Part 2 was executed here.

### Second pass (after lease 4 and website PRs 11 to 13)

Re-verified 2026-11-18 (evening) against website `origin/dev` `dad0a20` (PR 11 `afde392`, PR 12 `9431810`, PR 13 `dad0a20`), the handoff's lease 4 checkpoint and its "Real-export local rehearsal result" paragraph.

- Fenced commands re-matched against a fresh `dad0a20` source tree and the handoff: no new mismatches. The only differences from sources remain the intentional placeholders and adaptations listed above; the 2.13, 2.14 and 5.13 port changes now agree with the source, which moved to 8186. Files changed on dev that touch cited commands or paths (website repo `docs/member-lifecycle.md`, `docs/wordpress-import.md`, `docs/local-preview.md`, `identity/README.md`, `tools/release/README.md`, `web/README.md`, `web/tools/design-screenshots.mjs`, `cms/src/runtime-policy.ts`) were re-read; `design-screenshots.mjs` still accepts only a plain loopback origin. One cited command form was added: the production sweep invocation `cd /path/to/checkout/cms && bun run lifecycle:sweep` (5.17, quoted from website repo `docs/member-lifecycle.md`, not adapted).
- Lease statuses: every "IN PROGRESS (lease 4)" label replaced (6.2 to 6.6, 6.17, 7.8, 8.8, 9.19, the legend, 1.20) with "NOT YET PROVEN ON HETZNER: attempted in leases 3 and 4, not reached"; 6.5 and 9.19 now say teardown was proven by leases 2, 3 and 4; 6.6 rewritten as the lease 4 result and the decision on a fifth lease; 6.1 notes lease 4 repeated steps 1 to 3 and 8 and added nothing; the environments table notes the shared-lock dependency. Lease headings 6.2 to 6.5 no longer say "Lease 4".
- Status labels moved back up to PROVEN LOCALLY, or their evidence widened, on the new evidence (handoff, real-export local rehearsal 2026-11-18), 19 steps: 2.12, 2.13, 2.14, 4.2, 4.9, 4.10, 4.11, 5.9, 5.10, 5.13, 5.15, 5.16, 9.13, 9.14, 9.3, 5.8 (the form; production stays TESTED IN CODE ONLY), 5.12 (REST only), and 4.3 and 4.5 (already PROVEN LOCALLY, now also for the real plan). The CMS side of 2.14, 5.12 and 5.13 is REST only because the admin screens cannot do it yet (G-41). 5.17 stays TESTED IN CODE ONLY: the sweep was not part of the rehearsal. Net change since the draft: 2.1 and 5.17 down, 9.3 split, the other first-pass downgrades reversed with real evidence.
- Gaps: 0 removed. Added G-42 (builder-lock contention). G-41 rewritten from "flows never verified in a browser" (now verified) to the rehearsal's open defects. Reworded G-8, G-13 (dev `dad0a20`, nine migrations), G-14 and G-18 (PR 11 documents a source-checkout sweep invocation and a schedule; no packaged entrypoint or timer), G-19 (PR 11 documents the directory rules), G-36 (items 6, 9, 12 and the release README are fixed on the website; only item 3 remains). Conflicts 4, 6, 9 and 12 updated.
- Steps updated for PR 11 to 13: 4 intro and 4.8, 5.7 (single configuration table; a public CMS refuses to start without `LIFECYCLE_OUTBOX_DIR` and `LIFECYCLE_PUBLIC_ORIGIN`), 5.10 (same-account rule, retried mirror files, `mirror.undeliverable`), 5.17 (production invocation, daily schedule recommendation, `mirror.failed`), 6.12 to 6.14 (`dad0a20`, nine migrations, registry-derived ledger, Next.js 16.4.5), 2.2 and the header (`dad0a20`). Decision D-6 notes the recommended schedule.
- Not verified in this pass: the rehearsal numbers are quoted from the handoff (the rehearsal's private reports were not read); whether PR 11 to 13 fixed any defect other than the two named for PR 13; Womb docs were not re-checked against anything newer than trunk `01464be`.

### Third pass (independent review of the Womb PR)

- Lease statements made consistent everywhere: probes 1 to 4 (probe 2 ran twice) and known-full leases 1 to 4 all tore down cleanly with the baseline preserved; lease 1 stopped at the app plan, lease 2 at FORMAT, lease 3 at the services launch after install, reboot and networking, lease 4 at the first plan step. 6.1 keeps the install and repository-listener items, now citing the lease 3 record (`install`, `boot-installed`, `repository-start` passed at 01:15:40), and says the listener never started in lease 4.
- Removed the stale "whether the owner gave a yes for lease 4" line from the not-verified list; rewrote the first-person sentence in 1.9; prefixed the four website-repo documents (`local-preview`, `member-lifecycle`, `wordpress-import`, `wordpress-parity-plan`) with "website repo" throughout.
- Womb `docs/backlog.md`: items 3, 10 and 15 use `dad0a20`, item 10 and 15 now mention the real-export local rehearsal (no import to any real target) and that website PR 14 is open; item 13 stays on Womb #11 (it lists GitLab #8 as a source) and now also points to #12 for live integrations.
- The public snapshot is now produced by `make-runbook-snapshot.py` with the substitutions listed in the "Public snapshot" section below; it contains no workspace home paths, no private folder names and no builder home paths.

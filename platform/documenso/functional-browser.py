"""Disposable real-browser acceptance; private files only, fixed diagnostics.

create/verify are also reusable across an old-node-off restore. No response body,
callback URL, cookie, password, TOTP secret or recovery code is printed.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import struct
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DOC = "https://documents.terrace.invalid"
ROOT = Path("/root/documenso-functional")
stage = "private fixture validation"
ui_diagnostics = {"pageErrors": 0, "failedScripts": 0, "organisationStatus": 0, "organisationHeading": 0, "createButton": 0, "dialog": 0, "billingEnabled": 0, "documentStatus": 0, "documentRoute": 0}


LOGIN_COUNTERS = ("pageErrors", "failedRequests", "failedScripts", "authResponses", "authHttpErrors")


def fresh_login_diagnostics(ordinal):
    assert type(ordinal) is int and 1 <= ordinal <= 100
    return {"ordinal": ordinal, "step": "context", "exceptionClass": "unobserved",
            "signinStatus": 0, "callbackStatus": 0, "expectedErrorMatch": None,
            "sessionStatus": 0, "sessionEmpty": None,
            **{name: 0 for name in LOGIN_COUNTERS}}


def login_exception_class(error):
    # Return fixed labels only; exception messages/args can contain credentials.
    for kind in (AssertionError, ValueError, KeyError, TypeError):
        if type(error) is kind:
            return kind.__name__
    known = {("playwright._impl._errors", "TimeoutError"): "PlaywrightTimeout",
             ("playwright._impl._errors", "Error"): "PlaywrightError"}
    return known.get((type(error).__module__, type(error).__name__), "other")


def login_counter(facts, name):
    assert name in LOGIN_COUNTERS
    facts[name] = min(255, facts[name] + 1)


login_diagnostics = None


def private_read(path):
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0 and info.st_size <= 131072
    return json.loads(path.read_text())


def private_write(path, value):
    with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
        json.dump(value, output)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def totp(secret):
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    value = hmac.new(key, struct.pack(">Q", int(time.time()) // 30), hashlib.sha1).digest()
    offset = value[-1] & 15
    return str((struct.unpack(">I", value[offset:offset + 4])[0] & 0x7fffffff) % 1000000).zfill(6)


def pdf_fixture():
    # Protocol fixture, never a legal document or signing request.
    stream = b"BT /F1 12 Tf 40 100 Td (Synthetic rehearsal only - unsigned) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(index).encode() + b" 0 obj\n" + value + b"\nendobj\n"
    xref = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    result += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    return result + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def mode_options(arguments):
    if len(arguments) == 1 and arguments[0] in ["create", "sign", "verify", "disabled"]:
        return arguments[0], False
    if len(arguments) == 2 and arguments[0] in ["create", "verify"] and arguments[1] == "--signing":
        return arguments[0], True
    raise ValueError()


def main():
    global stage, login_diagnostics
    mode, signing = mode_options(sys.argv[1:])
    assert ROOT.stat().st_mode & 0o077 == 0
    users = {user["username"]: user for user in private_read(ROOT / "credentials.json")["users"]}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ["TERRACE_BROWSER_BINARY"],
                                             headless=True, args=["--no-sandbox"])
        active_page = [None]
        try:
            def context(state=None):
                value = browser.new_context(storage_state=state, locale="en-US")
                value.set_default_timeout(45000)
                return value

            def session(ctx, facts=None):
                if facts is not None:
                    facts["step"] = "session-request"
                response = ctx.request.get(DOC + "/api/auth/session")
                if facts is not None:
                    facts["sessionStatus"] = response.status
                    facts["step"] = "session-response"
                assert response.status == 200 and len(response.body()) <= 65536
                value = response.json()
                if facts is not None:
                    facts["sessionEmpty"] = isinstance(value, dict) and value.get("user") is None
                    facts["step"] = "session-assertion"
                return value

            def login(username, expected=None):
                global login_diagnostics
                ordinal = 1 if login_diagnostics is None else login_diagnostics["ordinal"] + 1
                facts = fresh_login_diagnostics(ordinal)
                login_diagnostics = facts
                ctx = context()
                page = ctx.new_page()
                active_page[0] = page
                # Per-page closures retain this attempt only, including the first
                # SSR/provider action; they never persist URLs, bodies or errors.
                page.on("pageerror", lambda _: login_counter(facts, "pageErrors"))
                page.on("requestfailed", lambda _: login_counter(facts, "failedRequests"))
                def login_response(response):
                    if response.request.resource_type == "script" and response.status >= 400:
                        login_counter(facts, "failedScripts")
                    parsed = urlsplit(response.url)
                    if parsed.netloc == "documents.terrace.invalid" and parsed.path.startswith("/api/auth/"):
                        login_counter(facts, "authResponses")
                        if response.status >= 400:
                            login_counter(facts, "authHttpErrors")
                page.on("response", login_response)
                facts["step"] = "signin-navigation"
                response = page.goto(DOC + "/signin")
                facts["signinStatus"] = response.status if response is not None else 0
                facts["step"] = "provider-click"
                page.get_by_role("button", name="Terrace", exact=True).click()
                facts["step"] = "identity-username"
                page.locator("#username").fill(username)
                facts["step"] = "identity-password"
                page.locator("#password").fill(users[username]["password"])
                facts["step"] = "identity-submit-and-callback"
                with page.expect_response(lambda response: urlsplit(response.url).netloc == "documents.terrace.invalid" and urlsplit(response.url).path == "/api/auth/callback/oidc") as callback:
                    page.locator("#kc-login").click()
                callback = callback.value
                facts["callbackStatus"] = callback.status
                facts["step"] = "application-redirect"
                page.wait_for_url(re.compile(r"^https://documents\.terrace\.invalid/"))
                if expected == "ACCOUNT_DISABLED":
                    facts["step"] = "disabled-response"
                    assert callback.status == 403 and len(callback.body()) <= 65536
                    code = callback.json()["code"]
                    facts["expectedErrorMatch"] = code == expected
                    assert code == expected
                    assert session(ctx, facts)["user"] is None
                elif expected:
                    facts["step"] = "expected-denial-status"
                    assert callback.status == 302
                    facts["step"] = "expected-denial-redirect"
                    page.wait_for_url(re.compile(r"[?&]error=" + expected + r"(?:&|$)"))
                    facts["step"] = "expected-denial-code"
                    errors = parse_qs(urlsplit(page.url).query).get("error")
                    facts["expectedErrorMatch"] = errors == [expected]
                    assert errors == [expected]
                    assert session(ctx, facts)["user"] is None
                else:
                    facts["step"] = "approved-status"
                    assert callback.status == 302
                    assert session(ctx, facts)["user"]["email"] == users[username]["email"]
                    facts["step"] = "session-cookie"
                    assert any(cookie["secure"] and cookie["httpOnly"] and "session" in cookie["name"].lower()
                               for cookie in ctx.cookies(DOC))
                facts["step"] = "passed"
                return ctx, page

            def post(ctx, suffix, value):
                response = ctx.request.post(DOC + "/api/auth/two-factor/" + suffix, data=value,
                                            headers={"Origin": DOC})
                assert response.status == 200 and len(response.body()) <= 65536
                result = response.json()
                assert result["success"] is True
                return result

            def download(ctx, path, save=False):
                assert re.fullmatch(r"/api/files/envelope/[A-Za-z0-9_-]+/envelopeItem/[A-Za-z0-9_-]+/download/(original|signed)", path)
                for attempt in range(30):
                    response = ctx.request.get(DOC + path, timeout=5000)
                    if response.status != 404 or not save:
                        break
                    time.sleep(0.5)
                assert response.status == 200
                value = response.body()
                assert 100 < len(value) <= 2 * 1024 * 1024 and value.startswith(b"%PDF-")
                assert "no-store" in response.headers.get("cache-control", "")
                assert "attachment" in response.headers.get("content-disposition", "")
                if save:
                    with open(ROOT / "signed.pdf", "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
                        output.write(value)
                return hashlib.sha256(value).hexdigest()

            def verify_signature():
                result = subprocess.run([sys.executable, "/etc/documenso-signing-proof.py", "verify"],
                                        capture_output=True, text=True, timeout=30)
                assert result.returncode == 0 and result.stdout.strip() == "Native signed PDF verified against the fixture certificate; altered bytes rejected."
                (ROOT / "signed.pdf").unlink()

            def denied_download(ctx, path, status):
                response = ctx.request.get(DOC + path)
                assert response.status == status and len(response.body()) <= 65536
                assert not response.body().startswith(b"%PDF-")
                assert "application/pdf" not in response.headers.get("content-type", "")
                assert "attachment" not in response.headers.get("content-disposition", "")

            if mode == "sign":
                stage = "native recipient email link"
                recipient = private_read(ROOT / "recipient.json")
                path = recipient["path"]
                assert re.fullmatch(r"/sign/[A-Za-z0-9_-]{16,256}", path)
                signer = context()
                page = signer.new_page()
                active_page[0] = page
                page.goto(DOC + path)
                # v2's h3 accessible name includes its remaining-field badge
                # and sidebar button; the actual private page confirms both.
                page.get_by_role("heading", name=re.compile(r"^Sign Document\b")).wait_for()
                page.get_by_role("button", name="Next Field", exact=True).wait_for()
                stage = "native typed signature and recipient field"
                page.get_by_test_id("signature-pad-dialog-button").click()
                page.get_by_role("tab", name="Type", exact=True).click()
                page.get_by_test_id("signature-pad-type-input").fill("Synthetic Signature")
                page.get_by_role("button", name="Next", exact=True).click()
                page.get_by_role("dialog").wait_for(state="hidden")
                # v2 renders fields on Konva, not v1 DOM field buttons. Its
                # native Next Field action locates the exact unsigned field.
                page.get_by_role("button", name="Next Field", exact=True).click()
                field = page.locator("#field-tooltip")
                field.wait_for(state="visible")
                assert field.count() == 1
                field.scroll_into_view_if_needed()
                box = field.bounding_box()
                assert box and 0 < box["width"] < 2048 and 0 < box["height"] < 2048
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                # The native completion action appears only after required
                # fields are inserted; keep that assertion before completion.
                page.get_by_role("button", name="Complete", exact=True).wait_for()
                stage = "native recipient completion and sealing"
                page.get_by_role("button", name="Complete", exact=True).click()
                page.get_by_role("dialog").get_by_role("button", name="Sign", exact=True).click()
                page.wait_for_url(lambda url: urlsplit(url).path == path + "/complete")
                signer.close()
                stage = "native sealed document download"
                member, page = login("local-member")
                original = private_read(ROOT / "receipt.json")["path"]
                signed_path = original.removesuffix("original") + "signed"
                fingerprint = download(member, signed_path, save=True)
                stage = "independent sealed PDF signature verification"
                verify_signature()
                assert fingerprint != private_read(ROOT / "receipt.json")["sha256"]
                private_write(ROOT / "signed-receipt.json", {"path": signed_path, "sha256": fingerprint})
                stage = "sealed PDF anonymous and non-team denial"
                anonymous = context()
                denied_download(anonymous, signed_path, 401)
                anonymous.close()
                alumni = context(str(ROOT / "alumni-session.json"))
                assert session(alumni)["user"]["email"] == users["local-alumni"]["email"]
                denied_download(alumni, signed_path, 403)
                alumni.close()
                member.close()
                (ROOT / "recipient.json").unlink()
            elif mode == "disabled":
                stage = "revoked existing session"
                old = context(str(ROOT / "alumni-session.json"))
                assert session(old)["user"] is None
                old.close()
                stage = "disabled user fresh OIDC denial"
                denied, _ = login("local-alumni", "ACCOUNT_DISABLED")
                denied.close()
                (ROOT / "alumni-session.json").unlink()
            else:
                stage = "closed signup and unapproved account denial"
                for username in ["local-unapproved", "local-editorial-editor"]:
                    denied, _ = login(username, "SIGNUP_DISABLED")
                    denied.close()
                stage = "approved administrator OIDC"
                admin, page = login("local-editorial-admin")
                assert "ADMIN" in session(admin)["user"]["roles"]
                page.goto(DOC + "/admin/users")
                page.get_by_role("heading", name="Manage users", exact=True).wait_for()
                admin.close()
                stage = "approved member OIDC"
                member, page = login("local-member")
                assert session(member)["user"]["roles"] == ["USER"]
                page.goto(DOC + "/admin/users")
                page.wait_for_url(lambda url: urlsplit(url).path != "/admin/users")
                def page_error(_):
                    ui_diagnostics["pageErrors"] += 1
                def script_response(response):
                    if response.request.resource_type == "script" and response.status >= 400:
                        ui_diagnostics["failedScripts"] += 1
                page.on("pageerror", page_error)
                page.on("response", script_response)
                if mode == "create":
                    stage = "encrypted two factor setup and decryption"
                    secret = post(member, "setup", {})["secret"]
                    codes = post(member, "enable", {"code": totp(secret)})["recoveryCodes"]
                    assert isinstance(codes, list) and len(codes) >= 8
                    assert digest(post(member, "view-recovery-codes", {"token": totp(secret)})["backupCodes"]) == digest(codes)
                    # The pinned English catalogue translates Organisation to Organization.
                    stage = "native organisation settings page"
                    response = page.goto(DOC + "/settings/organisations")
                    ui_diagnostics["organisationStatus"] = response.status
                    assert response.status == 200
                    page.get_by_role("heading", name="Organizations", exact=True).wait_for()
                    ui_diagnostics["organisationHeading"] = 1
                    ui_diagnostics["billingEnabled"] = int(page.evaluate("window.__ENV__?.NEXT_PUBLIC_FEATURE_BILLING_ENABLED === 'true'"))
                    stage = "native organisation create button"
                    page.get_by_role("button", name="Create organization", exact=True).wait_for()
                    ui_diagnostics["createButton"] = 1
                    page.get_by_role("button", name="Create organization", exact=True).click()
                    stage = "native organisation dialog"
                    page.get_by_role("dialog").wait_for()
                    ui_diagnostics["dialog"] = 1
                    stage = "native organisation name field"
                    page.get_by_label("Organization Name").fill("Synthetic Rehearsal")
                    stage = "native organisation form submission"
                    page.get_by_test_id("dialog-create-organisation-button").click()
                    stage = "native organisation list refresh"
                    page.get_by_role("dialog").wait_for(state="hidden")
                    page.reload()
                    page.locator('a[href^="/o/"]').filter(has_text="Synthetic Rehearsal").first.wait_for()
                    href = page.locator('a[href^="/o/"]').filter(has_text="Synthetic Rehearsal").first.get_attribute("href")
                    assert re.fullmatch(r"/o/[a-zA-Z0-9_-]+", href)
                    stage = "native team creation"
                    page.goto(DOC + href + "/settings/teams")
                    page.get_by_role("button", name="Create team", exact=True).first.click()
                    page.get_by_label("Team Name").fill("Synthetic Rehearsal Team")
                    page.get_by_label("Team URL").fill("rehearsal-team")
                    stage = "native team form submission"
                    page.get_by_test_id("dialog-create-team-button").click()
                    page.get_by_role("dialog").wait_for(state="hidden")
                    stage = "native documents listing navigation"
                    response = page.goto(DOC + "/t/rehearsal-team/documents")
                    ui_diagnostics["documentStatus"] = response.status
                    ui_diagnostics["documentRoute"] = int(urlsplit(page.url).path == "/t/rehearsal-team/documents" and urlsplit(page.url).netloc == "documents.terrace.invalid")
                    assert response.status == 200 and ui_diagnostics["documentRoute"] == 1
                    stage = "native unsigned PDF upload"
                    page.get_by_test_id("document-upload-input").first.set_input_files({"name": "synthetic-unsigned.pdf", "mimeType": "application/pdf", "buffer": pdf_fixture()})
                    page.wait_for_url(re.compile(r"/t/rehearsal-team/documents/.+/edit"))
                    stage = "native original PDF download"
                    # v2 starts at Document & Recipients; its PDF canvas belongs
                    # to a later editor step. Exercise the actual download action.
                    page.get_by_role("button", name="Download PDF", exact=True).click()
                    page.get_by_role("dialog").wait_for()
                    with page.expect_response(lambda response: urlsplit(response.url).netloc == "documents.terrace.invalid" and urlsplit(response.url).path.endswith("/download/original")) as pdf_response:
                        with page.expect_download() as pending_download:
                            page.get_by_role("dialog").get_by_role("button", name="Original", exact=True).click()
                    response = pdf_response.value
                    parsed = urlsplit(response.url)
                    assert response.status == 200 and not parsed.query and not parsed.fragment
                    path = parsed.path
                    downloaded = pending_download.value
                    assert downloaded.failure() is None
                    artifact = Path(downloaded.path())
                    assert 100 < artifact.stat().st_size <= 2 * 1024 * 1024
                    content = artifact.read_bytes()
                    assert content.startswith(b"%PDF-")
                    baseline = hashlib.sha256(content).hexdigest()
                    assert baseline == download(member, path)
                    downloaded.delete()
                    private_write(ROOT / "receipt.json", {"path": path, "sha256": baseline, "totpSecret": secret, "backupCodesHash": digest(codes)})
                    page.keyboard.press("Escape")
                    page.get_by_role("dialog").wait_for(state="hidden")
                    if signing:
                        stage = "native signing recipient and signature field"
                        page.get_by_role("button", name="Add Myself", exact=True).click()
                        assert page.get_by_test_id("signer-email-input").first.input_value() == users["local-member"]["email"]
                        page.get_by_test_id("envelope-editor-step-addFields").first.click()
                        page.get_by_role("button", name="Signature", exact=True).click()
                        page.locator(".konva-container canvas").first.click(position={"x": 120, "y": 140})
                        page.get_by_text("1 Field", exact=True).wait_for()
                        page.get_by_test_id("envelope-editor-step-upload").first.click()
                        stage = "native synthetic signing request distribution"
                        page.locator('button[title="Send Envelope"]').click()
                        page.get_by_role("heading", name="Send Document", exact=True).wait_for()
                        page.get_by_role("dialog").get_by_role("button", name="Send", exact=True).click()
                        page.get_by_role("dialog").wait_for(state="hidden")
                    stage = "approved non-team user and private session receipt"
                    alumni, _ = login("local-alumni")
                    denied_download(alumni, path, 403)
                    private_write(ROOT / "alumni-session.json", alumni.storage_state())
                    alumni.close()
                else:
                    stage = "retained document and encryption keys"
                    receipt = private_read(ROOT / "receipt.json")
                    path = receipt["path"]
                    assert download(member, path) == receipt["sha256"]
                    assert digest(post(member, "view-recovery-codes", {"token": totp(receipt["totpSecret"])})["backupCodes"]) == receipt["backupCodesHash"]
                    if signing:
                        signed = private_read(ROOT / "signed-receipt.json")
                        assert download(member, signed["path"], save=True) == signed["sha256"]
                        stage = "independent retained PDF signature verification"
                        verify_signature()
                stage = "anonymous document denial"
                anonymous = context()
                denied_download(anonymous, path, 401)
                anonymous.close()
                member.close()
            print("Documenso functional browser phase " + mode + " passed.")
        except BaseException:
            try:
                page = active_page[0]
                if page is not None and not page.is_closed():
                    # Fixed vocabulary only: actual accessible-label counts,
                    # never arbitrary DOM, text, field values or callback URLs.
                    roles = [("heading", "Organizations"), ("heading", "Organisations"), ("heading", "Documents"),
                             ("heading", "Team not found"), ("heading", "Page not found"), ("heading", "Unauthorized"),
                             ("button", "Create organization"), ("button", "Create organisation"),
                             ("button", "Create team"), ("button", "Continue"),
                             ("button", "Upload Document"), ("button", "Add Signer"),
                             ("button", "Send"), ("button", "Create")]
                    counts = {role + ":" + name: min(20, page.get_by_role(role, name=name, exact=True).count())
                              for role, name in roles}
                    for name in ["Organization Name", "Organisation Name", "Team Name", "Team URL", "Email", "Name"]:
                        counts["label:" + name] = min(20, page.get_by_label(name, exact=True).count())
                    counts["fileInputs"] = min(20, page.locator('input[type="file"]').count())
                    counts["uploadInputs"] = min(20, page.get_by_test_id("document-upload-input").count())
                    counts["dialogs"] = min(20, page.get_by_role("dialog").count())
                    print("Documenso bounded UI label counts: " + json.dumps(counts, sort_keys=True), file=sys.stderr)
                    # Private actual-page artifacts, exported only to the failed
                    # build's protected temporary shared directory (never $out).
                    labels = {}
                    for role in ["heading", "button"]:
                        names = []
                        for element in page.get_by_role(role).all()[:32]:
                            name = " ".join((element.get_attribute("aria-label", timeout=1000) or element.inner_text(timeout=1000)).split())
                            if 0 < len(name) <= 128 and not re.search(r"@|/|synthetic|local-|\.pdf", name, re.I):
                                names.append(name)
                        labels[role] = names
                    private_write(ROOT / ("failure-" + mode + "-labels.json"), labels)
                    masks = [page.locator("input, textarea, canvas, iframe, object, embed, img, script, pre, code, a[href], .react-pdf__Page, .textLayer, [contenteditable]"),
                             page.get_by_text(re.compile(r"@|://|terrace\.invalid|/(?:t|api|auth|realms|settings|admin)/|synthetic|local-", re.I))]
                    screenshot = page.screenshot(mask=masks, timeout=10000)
                    if len(screenshot) <= 4 * 1024 * 1024:
                        with open(ROOT / ("failure-" + mode + ".png"), "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
                            output.write(screenshot)
                    print("Documenso private failure artifacts captured; content suppressed.", file=sys.stderr)
            except BaseException:
                print("Documenso bounded UI label collection unavailable.", file=sys.stderr)
            raise
        finally:
            browser.close()


def entrypoint():
    try:
        main()
    except BaseException as error:
        if login_diagnostics is not None:
            login_diagnostics["exceptionClass"] = login_exception_class(error) if login_diagnostics["step"] != "passed" else "not-login"
            print("Documenso bounded login facts: " + json.dumps(login_diagnostics, sort_keys=True), file=sys.stderr)
        print("Documenso functional check failed at " + stage + "; private diagnostics suppressed.", file=sys.stderr)
        if stage != "private fixture validation":
            # Only fixed keys and integer counts/status; never page text/URLs/errors.
            print("Documenso UI diagnostic counts: " + json.dumps(ui_diagnostics, sort_keys=True), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()

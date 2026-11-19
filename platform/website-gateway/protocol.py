"""Guest-only synthetic HTTPS/OIDC checks. Never print responses or credentials."""
import http.cookiejar
import json
import os
import re
import ssl
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

WEB = "https://web.terrace.invalid"
CMS = "https://cms.terrace.invalid"
ID = "https://identity.terrace.invalid"
PRIVATE = Path("/var/lib/terrace-gateway-test")
stage = "start"
count = 0


def check(condition):
    global count
    if not condition:
        raise RuntimeError("Synthetic assertion rejected")
    count += 1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Client:
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def request(self, url, method="GET", data=None, headers=None):
        # All redirects/actions are checked before use; no arbitrary URL fetch.
        check(urllib.parse.urlsplit(url).netloc in [
            "web.terrace.invalid", "cms.terrace.invalid", "identity.terrace.invalid", "127.0.0.1:8088", "127.0.0.1:14294"
        ])
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(2 * 1024 * 1024 + 1)
            check(len(body) <= 2 * 1024 * 1024)
            return response.status, response.headers, body

    def api(self, url, method="GET", data=None, headers=None):
        headers = dict(headers or {})
        if data is not None:
            headers["Content-Type"] = "application/json"
        status, response_headers, body = self.request(
            url, method, json.dumps(data).encode() if data is not None else None, headers
        )
        return status, response_headers, json.loads(body) if body else None


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields = {}
        self.inside = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "kc-form-login":
            self.action = attrs.get("action")
            self.inside = True
        if self.inside and tag == "input" and attrs.get("name") and attrs.get("type") == "hidden":
            self.fields[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


def private(headers):
    value = headers.get("Cache-Control", "").lower()
    check("private" in value and "no-store" in value and "public" not in value)


def seed():
    global stage
    stage = "private fixture enrollment"
    credentials = json.loads((PRIVATE / "credentials.json").read_text())
    client = Client()
    status, _, body = client.request(
        "http://127.0.0.1:8088/realms/master/protocol/openid-connect/token", "POST",
        urllib.parse.urlencode({"client_id": "admin-cli", "grant_type": "password",
                                "username": "gateway-fixture-bootstrap", "password": credentials["adminPassword"]}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    check(status == 200)
    token = json.loads(body)["access_token"]
    admin_headers = {"Authorization": "Bearer " + token}
    base = "http://127.0.0.1:8088/admin/realms/terrace/"
    for user in credentials["users"]:
        status, _, _ = client.api(base + "users", "POST", {
            "username": user["username"], "enabled": True, "emailVerified": True,
            "email": user["username"] + "@terrace.invalid", "firstName": "Synthetic", "lastName": "Fixture",
            "credentials": [{"type": "password", "value": user["password"], "temporary": False}],
        }, admin_headers)
        check(status == 201)
        status, _, users = client.api(base + "users?" + urllib.parse.urlencode({"username": user["username"], "exact": "true"}), headers=admin_headers)
        check(status == 200 and len(users) == 1)
        for role in user["roles"]:
            status, _, definition = client.api(base + "roles/" + role, headers=admin_headers)
            check(status == 200)
            status, _, _ = client.api(base + "users/" + users[0]["id"] + "/role-mappings/realm", "POST", [definition], admin_headers)
            check(status == 204)
    # The imported member-linking contract: verified, unique, administrator-only email,
    # and the email claim on the member client only.
    status, _, realm = client.api(base[:-1], headers=admin_headers)
    check(status == 200 and realm["verifyEmail"] is True and realm["duplicateEmailsAllowed"] is False and realm["editUsernameAllowed"] is False)
    status, _, profile = client.api(base + "users/profile", headers=admin_headers)
    check(status == 200 and [attribute["permissions"]["edit"] for attribute in profile["attributes"] if attribute["name"] == "email"] == [["admin"]])
    for client_id, expected in [("terrace-web", True), ("terrace-cms", False)]:
        status, _, found = client.api(base + "clients?" + urllib.parse.urlencode({"clientId": client_id}), headers=admin_headers)
        check(status == 200 and len(found) == 1)
        status, _, scopes = client.api(base + "clients/" + found[0]["id"] + "/default-client-scopes", headers=admin_headers)
        check(status == 200 and ("email" in [scope["name"] for scope in scopes]) == expected)
    for user in credentials["users"]:
        status, _, found = client.api(base + "users?" + urllib.parse.urlencode({"username": user["username"], "exact": "true"}), headers=admin_headers)
        check(status == 200 and found[0]["emailVerified"] is True and found[0]["email"] == user["username"] + "@terrace.invalid")
    master = "http://127.0.0.1:8088/admin/realms/master/users"
    status, _, users = client.api(master + "?username=gateway-fixture-bootstrap&exact=true", headers=admin_headers)
    check(status == 200 and len(users) == 1)
    status, _, _ = client.api(master + "/" + users[0]["id"], "DELETE", headers=admin_headers)
    check(status == 204)
    print("Disposable identity enrollment passed; temporary administrator removed.")


def sign_in(username, component="cms", tamper=False):
    client = Client()
    origin = CMS if component == "cms" else WEB
    callback = origin + ("/api/editorial/callback" if component == "cms" else "/auth/callback")
    status, headers, _ = client.request(
        origin + ("/api/editorial/login" if component == "cms" else "/auth/login"),
        "POST" if component == "cms" else "GET", headers={"Origin": origin,
        "X-Forwarded-Host": "attacker.invalid", "X-Forwarded-Proto": "http", "Forwarded": "host=attacker.invalid;proto=http"},
    )
    check(status == 303)
    private(headers)
    authorization = headers["Location"]
    parsed = urllib.parse.urlsplit(authorization)
    query = urllib.parse.parse_qs(parsed.query)
    check(parsed.scheme == "https" and parsed.netloc == "identity.terrace.invalid")
    check(query["redirect_uri"] == [callback] and query["code_challenge_method"] == ["S256"])
    check(bool(query.get("state")) and bool(query.get("nonce")))
    for _ in range(8):
        status, headers, body = client.request(authorization)
        if status not in [301, 302, 303]:
            break
        authorization = urllib.parse.urljoin(authorization, headers["Location"])
    check(status == 200)
    form = LoginForm()
    form.feed(body.decode())
    check(form.action and form.action.startswith(ID + "/realms/terrace/login-actions/"))
    credentials = json.loads((PRIVATE / "credentials.json").read_text())
    user = next(user for user in credentials["users"] if user["username"] == username)
    fields = {**form.fields, "username": username, "password": user["password"], "credentialId": ""}
    status, headers, _ = client.request(form.action, "POST", urllib.parse.urlencode(fields).encode(),
                                        {"Content-Type": "application/x-www-form-urlencoded"})
    check(status in [302, 303])
    destination = headers["Location"]
    check(destination.startswith(callback + "?"))
    if tamper:
        parsed = urllib.parse.urlsplit(destination)
        query = urllib.parse.parse_qs(parsed.query)
        query["state"] = ["wrong-state"]
        destination = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))
    status, headers, _ = client.request(destination)
    private(headers)
    return client, status


def test():
    global stage
    stage = "HTTPS editorial authorization code flow"
    editor, status = sign_in("local-editorial-editor")
    check(status == 303)
    status, headers, me = editor.api(CMS + "/api/users/me")
    check(status == 200 and me["user"]["role"] == "editor" and not me.get("token"))
    private(headers)
    sessions = [cookie for cookie in editor.cookies if cookie.name.startswith("terrace_cms_") and not cookie.name.endswith("_login")]
    check(len(sessions) == 1 and sessions[0].secure and "HttpOnly" in sessions[0]._rest)
    check(sessions[0]._rest.get("SameSite") == "Lax")
    anonymous = Client()
    check(anonymous.api(CMS + "/api/users/me")[2].get("user") is None)
    for username in ["local-member", "local-unapproved"]:
        denied, _ = sign_in(username)
        check(denied.api(CMS + "/api/users/me")[2].get("user") is None)
    invalid, _ = sign_in("local-editorial-editor", tamper=True)
    check(invalid.api(CMS + "/api/users/me")[2].get("user") is None)

    stage = "publication and cross-origin mutation"
    suffix = os.environ.get("TERRACE_SYNTHETIC_PHASE", "")
    check(suffix in ["", "-restored"])
    event = {"slug": "gateway-synthetic-event" + suffix, "title": "Synthetic gateway event", "summary": "Disposable fixture",
             "startsAt": "2030-01-18T23:00:00.000Z", "category": "music", "locationName": "Test only", "_status": "draft"}
    check(editor.api(CMS + "/api/events", "POST", event, {"Origin": "https://attacker.invalid"})[0] == 403)
    status, _, created = editor.api(CMS + "/api/events", "POST", event, {"Origin": CMS})
    check(status == 201)
    event_id = str(created["doc"]["id"])
    detail = WEB + "/events/" + event["slug"] + "/"
    check(anonymous.request(detail)[0] == 404)
    check(editor.api(CMS + "/api/events/" + event_id, "PATCH", {"_status": "published"}, {"Origin": CMS})[0] == 200)
    status, headers, body = anonymous.request(detail)
    check(status == 200 and b"Synthetic gateway event" in body)
    private(headers)
    check(editor.api(CMS + "/api/events/" + event_id, "PATCH", {"_status": "draft"}, {"Origin": CMS})[0] == 200)
    check(anonymous.request(detail)[0] == 404)

    stage = "authenticated then anonymous private media cache"
    status, headers, body = anonymous.request(CMS + "/_next/image?url=%2Fapi%2Fmedia%2Ffile%2Fpublic.png&w=64&q=75")
    check(status == 200 and headers.get("Content-Type", "").startswith("image/") and len(body) > 0)
    private(headers)
    private_file = CMS + "/api/media/file/private.png"
    status, headers, private_bytes = editor.request(private_file)
    check(status == 200 and headers.get("Content-Type", "").startswith("image/"))
    private(headers)
    optimizer = CMS + "/_next/image?url=%2Fapi%2Fmedia%2Ffile%2Fprivate.png&w=64&q=75"
    status, headers, _ = editor.request(optimizer)
    check(status in [200, 400, 403])
    private(headers)
    for url in [optimizer, private_file, optimizer]:
        status, headers, body = anonymous.request(url)
        check(status != 200 and body != private_bytes and not headers.get("Content-Type", "").startswith("image/"))
        private(headers)

    stage = "HTTPS member flow and logout"
    member, status = sign_in("local-member", "web")
    check(status == 303)
    status, headers, body = member.request(WEB + "/members/")
    check(status == 200 and b"Pull up a chair" in body)
    private(headers)
    check(any(cookie.name == "terrace_member" and cookie.secure and "HttpOnly" in cookie._rest for cookie in member.cookies))
    check(member.request(WEB + "/auth/logout", "POST", headers={"Origin": "https://attacker.invalid"})[0] == 403)
    check(member.request(WEB + "/auth/logout", "POST", headers={"Origin": WEB})[0] == 303)
    check(member.request(WEB + "/members/")[0] == 303)
    outsider, status = sign_in("local-unapproved", "web")
    check(status == 403 and outsider.request(WEB + "/members/")[0] == 303)
    check(editor.request(CMS + "/api/editorial/logout", "POST", headers={"Origin": CMS})[0] == 303)
    check(editor.api(CMS + "/api/users/me")[2].get("user") is None)
    print(f"Native HTTPS protocol checks passed: {count} bounded assertions; SSO, roles, state, CSRF, cookies, publication, private cache and logout.")


OUTBOX = Path("/var/lib/terrace-cms-outbox")
POST_SLUG = "synthetic-imported-post"
APPLICANT = "local-alumni"


class ApplyForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "t" and attrs.get("value"):
            self.token = attrs["value"]


def content():
    global stage
    stage = "imported-style news through the gateway"
    anonymous = Client()
    status, headers, body = anonymous.request(WEB + "/news/")
    check(status == 200 and b"Synthetic imported post" in body)
    private(headers)
    status, headers, body = anonymous.request(WEB + "/news/" + POST_SLUG + "/")
    check(status == 200 and b"Synthetic imported post" in body and b"Synthetic imported body." in body)
    # CMS_PUBLIC_ORIGIN keeps only body images served by the public CMS origin.
    check(b'src="https://cms.terrace.invalid/api/media/file/public.png"' in body and b"elsewhere.invalid" not in body)
    private(headers)
    status, _, image = anonymous.request(CMS + "/api/media/file/public.png")
    check(status == 200 and len(image) > 0)
    stage = "old WordPress URL redirect"
    status, headers, _ = anonymous.request(WEB + "/2019/05/" + POST_SLUG + "/")
    check(status == 301 and urllib.parse.urlsplit(headers["Location"]).path == "/news/" + POST_SLUG + "/")
    check(anonymous.request(WEB + "/2019/05/synthetic-unknown-post/")[0] == 404)
    print(f"Imported-content checks passed: {count} bounded assertions; news list, imported post, public body image and old-URL redirect.")


APPLY_OUTCOMES = [("limited", b"Too many applications from here"), ("moved", b"Your connection changed"),
                  ("reload", b"Please reload the page"), ("closed", b"Applications are not open"), ("unavailable", b"temporarily unavailable"),
                  ("fields", b"Please check the"), ("received", b"Your request was received.")]


def apply(attempts, base=WEB, accept="text/html"):
    """Open the form for every attempt, wait out the minimum fill time, then submit each in order.

    A browser's non-enhanced form post asks for text/html and gets the action's HTTP status (fail(429) is 429).
    Without that Accept header SvelteKit answers with its JSON action protocol: HTTP 200 and the result inside.
    """
    global stage
    stage = "application form through the gateway" if base == WEB else "application form on the private loopback port"
    tokens = []
    for email, forged in attempts:
        client = Client()
        status, headers, body = client.request(base + "/apply/", headers={"X-Forwarded-For": forged, "X-Real-IP": forged})
        check(status == 200)
        private(headers)
        form = ApplyForm()
        form.feed(body.decode())
        check(form.token is not None)
        tokens.append((client, form.token, email, forged))
    time.sleep(3.5)
    statuses, outcomes = [], []
    for client, token, email, forged in tokens:
        fields = {"t": token, "website": "", "name": "Synthetic Applicant", "affiliation": "alumni", "classYear": "",
                  "contactEmail": email, "note": "Synthetic rehearsal application."}
        status, headers, body = client.request(base + "/apply/", "POST", urllib.parse.urlencode(fields).encode(), {
            "Content-Type": "application/x-www-form-urlencoded", "Accept": accept, "Origin": WEB, "X-Forwarded-For": forged, "X-Real-IP": forged})
        private(headers)
        statuses.append(status)
        if accept == "application/json":
            result = json.loads(body)
            outcomes.append((result.get("type"), result.get("status")))
        else:
            # Fixed application wording only (never submitted values): which outcome the page reported.
            outcomes.append(next((label for label, text in APPLY_OUTCOMES if text in body), "other"))
    stage += f": statuses {statuses} outcomes {outcomes}"
    return statuses, outcomes


def apply_burst():
    # Six submissions from one real address (the gateway guest), each forging a different client address.
    attempts = [(APPLICANT + "@terrace.invalid", "203.0.113.10")] + [(f"burst-{index}@terrace.invalid", f"198.51.{index}.7") for index in range(1, 6)]
    check(apply(attempts) == ([200] * 5 + [429], ["received"] * 5 + ["limited"]))
    # The same exhausted bucket seen two other ways. Through the gateway with SvelteKit's JSON action protocol
    # (no text/html Accept): HTTP 200 carrying the 429 failure. Directly on the private loopback port, with the
    # header the gateway would write: the same 429 page, so the gateway passes the application's status through.
    check(apply([("burst-json@terrace.invalid", "192.0.2.1")], accept="application/json") == ([200], [("failure", 429)]))
    check(apply([("burst-direct@terrace.invalid", "127.0.0.1")], base="http://127.0.0.1:14294") == ([429], ["limited"]))
    print(f"Application rate-limit checks passed: {count} bounded assertions; five accepted, sixth limited (HTTP 429 through the gateway and on the loopback port) despite forged client headers.")


def apply_other():
    # A different real client address, forging the exhausted address, still has its own bucket.
    check(apply([("other-network@terrace.invalid", "127.0.0.1")]) == ([200], ["received"]))
    print(f"Separate-address application check passed: {count} bounded assertions.")


def staff(username, role):
    client, status = sign_in(username)
    check(status == 303)
    status, _, me = client.api(CMS + "/api/users/me")
    check(status == 200 and me["user"]["role"] == role)
    return client


def application(client, email):
    query = urllib.parse.urlencode({"where[contactEmail][equals]": email, "depth": "0", "limit": "2"})
    status, headers, found = client.api(CMS + "/api/member-applications?" + query)
    check(status == 200 and len(found["docs"]) == 1)
    private(headers)
    return found["docs"][0]


def invitation_file(email):
    matches = []
    for path in sorted(OUTBOX.glob("*-invitation.json")):
        message = json.loads(path.read_text())
        if message.get("to") == email:
            matches.append(message)
    check(len(matches) == 1)
    return matches[0]


def approve():
    global stage
    stage = "editor approval issues an invitation"
    editor = staff("local-editorial-editor", "editor")
    email = APPLICANT + "@terrace.invalid"
    pending = application(editor, email)
    check(pending["status"] == "pending")
    status, _, _ = editor.api(CMS + "/api/member-applications/" + str(pending["id"]), "PATCH",
                              {"status": "approved", "grantRole": "alumni"}, {"Origin": "https://attacker.invalid"})
    check(status == 403)
    status, _, _ = editor.api(CMS + "/api/member-applications/" + str(pending["id"]), "PATCH",
                              {"status": "approved", "grantRole": "alumni"}, {"Origin": CMS})
    check(status == 200 and application(editor, email)["status"] == "approved")
    message = invitation_file(email)
    check(re.fullmatch(r"https://web\.terrace\.invalid/join/#[A-Za-z0-9_-]{20,}", re.search(r"https://\S+", message["body"])[0]) is not None)
    print(f"Approval checks passed: {count} bounded assertions; invitation mirror written to the private outbox.")


def redeem():
    global stage
    stage = "invitation redemption and first member sign-in"
    email = APPLICANT + "@terrace.invalid"
    link = re.search(r"https://web\.terrace\.invalid/join/#([A-Za-z0-9_-]+)", invitation_file(email)["body"])
    check(link is not None)
    anonymous = Client()
    # Single use: the first post accepts it, the repeat is refused (the page states which; status stays 200).
    for accepted in [True, False]:
        status, headers, body = anonymous.request(WEB + "/join/", "POST", urllib.parse.urlencode({"token": link[1]}).encode(),
                                                  {"Content-Type": "application/x-www-form-urlencoded", "Origin": WEB})
        check(status == 200 and (b"Invitation accepted." in body) == accepted and (b"That code was not accepted." in body) == (not accepted))
        private(headers)
    member, status = sign_in(APPLICANT, "web")
    check(status == 303)
    status, headers, _ = member.request(WEB + "/members/")
    check(status == 200)
    private(headers)
    # The CMS links the first sign-in to its application by the token's verified email.
    admin = staff("local-editorial-admin", "admin")
    linked = application(admin, email)
    check(linked["status"] == "approved" and isinstance(linked.get("member"), int))
    print(f"Redemption checks passed: {count} bounded assertions; single-use invitation and automatic application link.")


def erase():
    global stage
    stage = "administrator offboarding and erasure"
    email = APPLICANT + "@terrace.invalid"
    admin = staff("local-editorial-admin", "admin")
    linked = application(admin, email)
    member_id = linked["member"]
    check(isinstance(member_id, int))
    status, _, _ = admin.api(CMS + "/api/member-offboardings", "POST", {"member": member_id, "reason": "Synthetic rehearsal"}, {"Origin": CMS})
    check(status == 201)
    status, _, _ = admin.api(CMS + "/api/data-requests", "POST", {"member": member_id, "kind": "erasure"}, {"Origin": CMS})
    check(status == 201)
    query = urllib.parse.urlencode({"where[contactEmail][equals]": email, "limit": "1"})
    check(admin.api(CMS + "/api/member-applications?" + query)[2]["totalDocs"] == 0)
    # Erasure scrubs the linked application and withdraws it.
    status, _, scrubbed = admin.api(CMS + "/api/member-applications/" + str(linked["id"]) + "?depth=0")
    check(status == 200 and scrubbed["status"] == "withdrawn" and scrubbed["name"] == "Erased applicant")
    check(scrubbed["contactEmail"] != email and not scrubbed.get("note"))
    erased, status = sign_in(APPLICANT, "web")
    check(erased.request(WEB + "/members/")[0] != 200)
    print(f"Erasure checks passed: {count} bounded assertions; offboarded, erased, application scrubbed and member access closed.")


ACTIONS = {"seed": seed, "test": test, "content": content, "apply-burst": apply_burst, "apply-other": apply_other,
           "approve": approve, "redeem": redeem, "erase": erase}

try:
    if len(sys.argv) != 2 or sys.argv[1] not in ACTIONS:
        raise RuntimeError("Invalid action")
    ACTIONS[sys.argv[1]]()
except BaseException:
    # HTTP/form/parser errors can carry tokens, passwords or callback URLs.
    print(f"Native HTTPS protocol check failed at {stage}; {count} bounded checks completed.", file=sys.stderr)
    sys.exit(1)

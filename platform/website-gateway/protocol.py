"""Guest-only synthetic HTTPS/OIDC checks. Never print responses or credentials."""
import http.cookiejar
import json
import os
import ssl
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
            "web.terrace.invalid", "cms.terrace.invalid", "identity.terrace.invalid", "127.0.0.1:8088"
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


try:
    if sys.argv[1:] == ["seed"]:
        seed()
    elif sys.argv[1:] == ["test"]:
        test()
    else:
        raise RuntimeError("Invalid action")
except BaseException:
    # HTTP/form/parser errors can carry tokens, passwords or callback URLs.
    print(f"Native HTTPS protocol check failed at {stage}; {count} bounded checks completed.", file=sys.stderr)
    sys.exit(1)

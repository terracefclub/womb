"""Rehearsal-only probe for the identity administration VM proof. Never prints a value.

  signin USERNAME PASSWORD_FILE [NEW_PASSWORD_FILE]  editorial sign-in at the CMS through the HTTPS
                                                     gateway; completes a forced password change
  introspect CLIENT SECRET_FILE                      "200", or the refusal's HTTP status and OAuth error code
  token USERNAME PASSWORD_FILE [TOKEN_FILE]          the same for a master admin-cli password grant; with
                                                     TOKEN_FILE, the access token is kept there (root-only)
  half-admin BOOTSTRAP_PASSWORD_FILE USERNAME [PASSWORD_FILE [admin]]
                                                     as the bootstrap account, create USERNAME in master with no
                                                     role and no password (what a crash after POST leaves), or
                                                     with a password of its own (not the tool's initial label)
                                                     and optionally the admin role
  delete-user ADMIN PASSWORD_FILE USERNAME           as ADMIN, delete USERNAME from master
"""
import base64
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

CMS = "https://cms.terrace.invalid"
ID = "https://identity.terrace.invalid"
BACKEND = "http://127.0.0.1:8088"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self.current = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"id": attrs.get("id"), "action": attrs.get("action"), "hidden": {}, "inputs": []}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and attrs.get("name"):
            self.current["inputs"].append(attrs["name"])
            if attrs.get("type") == "hidden":
                self.current["hidden"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def read(path):
    with open(path) as handle:
        return handle.read().strip()


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPCookieProcessor(self.jar),
                                                  # The test CA by store-path reference (SSL_CERT_FILE), never a system bundle.
                                                  urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))))

    def request(self, url, method="GET", data=None, headers=None):
        host = urllib.parse.urlsplit(url).netloc
        if host not in ("cms.terrace.invalid", "identity.terrace.invalid", "127.0.0.1:8088"):
            raise SystemExit("probe: unexpected host")
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            response = self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read(2 * 1024 * 1024)


def form_post(client, form, fields):
    data = urllib.parse.urlencode({**form["hidden"], **fields}).encode()
    if not form["action"] or not form["action"].startswith(ID + "/realms/terrace/"):
        raise SystemExit("probe: unexpected form action")
    return client.request(form["action"], "POST", data, {"Content-Type": "application/x-www-form-urlencoded"})


def settle(client, status, headers, body):
    """Follow Keycloak's own redirects (for example to a required action) until a page or a redirect elsewhere."""
    for _ in range(8):
        if status not in (302, 303) or not headers["Location"].startswith(ID + "/realms/terrace/"):
            break
        status, headers, body = client.request(headers["Location"])
    return status, headers, body


def fail(stage):
    print(f"sign-in failed at {stage}")
    sys.exit(2)


def signin(username, password_file, new_password_file=None):
    if not os.environ.get("SSL_CERT_FILE"):
        raise SystemExit("probe: set SSL_CERT_FILE to the test CA (HTTPS sign-in)")
    client = Client()
    status, headers, _ = client.request(CMS + "/api/editorial/login", "POST", headers={"Origin": CMS})
    if status != 303:
        fail("login start")
    location = headers["Location"]
    for _ in range(8):
        status, headers, body = client.request(location)
        if status not in (301, 302, 303):
            break
        location = urllib.parse.urljoin(location, headers["Location"])
    if status != 200:
        fail("login page")
    parser = Forms()
    parser.feed(body.decode())
    login = [form for form in parser.forms if form["id"] == "kc-form-login"]
    if not login:
        fail("login form")
    status, headers, body = settle(client, *form_post(client, login[0], {"username": username, "password": read(password_file), "credentialId": ""}))
    if status == 200:
        parser = Forms()
        parser.feed(body.decode())
        update = [form for form in parser.forms if "password-new" in form["inputs"]]
        if not update or not new_password_file:
            fail("credentials" if not update else "forced password change (no new password given)")
        new = read(new_password_file)
        status, headers, body = settle(client, *form_post(client, update[0], {"password-new": new, "password-confirm": new}))
        print("password changed at first sign-in")
    if status not in (302, 303) or not headers["Location"].startswith(CMS + "/api/editorial/callback?"):
        fail("callback redirect")
    status, _, _ = client.request(headers["Location"])
    if status not in (302, 303):
        fail("callback")
    status, _, body = client.request(CMS + "/api/users/me")
    if status != 200:
        fail("session")
    user = (json.loads(body) or {}).get("user") or {}
    if not user.get("role"):
        fail("session user")
    print(f"signed-in role={user['role']}")


def outcome(status, body):
    if status == 200:
        return "200"
    try:
        error = json.loads(body).get("error", "none")
    except ValueError:
        error = "unparsable"
    return f"{status} {error if isinstance(error, str) and error.replace('_', '').isalpha() else 'other'}"


def introspect(client_id, secret_file):
    credentials = base64.b64encode(f"{client_id}:{read(secret_file)}".encode()).decode()
    status, _, body = Client().request(BACKEND + "/realms/terrace/protocol/openid-connect/token/introspect", "POST",
                                    urllib.parse.urlencode({"token": "probe"}).encode(),
                                    {"Content-Type": "application/x-www-form-urlencoded", "Authorization": "Basic " + credentials,
                                     "X-Forwarded-Proto": "https"})
    print(outcome(status, body))


def grant(username, password):
    form = urllib.parse.urlencode({"client_id": "admin-cli", "grant_type": "password", "username": username, "password": password}).encode()
    return Client().request(BACKEND + "/realms/master/protocol/openid-connect/token", "POST", form,
                            {"Content-Type": "application/x-www-form-urlencoded"})


def token(username, password_file, token_file=None):
    status, _, body = grant(username, read(password_file))
    if status == 200 and token_file:
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.loads(body)["access_token"] + "\n")
    print(outcome(status, body))


def bootstrap_headers(bootstrap_file, admin="terrace-bootstrap"):
    status, _, body = grant(admin, read(bootstrap_file))
    if status != 200:
        raise SystemExit("probe: bootstrap grant refused")
    return {"Authorization": "Bearer " + json.loads(body)["access_token"], "Content-Type": "application/json"}


def half_admin(bootstrap_file, username, password_file=None, role=None):
    headers = bootstrap_headers(bootstrap_file)
    user = {"username": username, "enabled": True, "email": username + "@terrace.invalid", "firstName": "Half", "lastName": "Created", "emailVerified": True}
    if password_file:
        user["credentials"] = [{"type": "password", "value": read(password_file), "temporary": False}]
    status, _, _ = Client().request(BACKEND + "/admin/realms/master/users", "POST", json.dumps(user).encode(), headers)
    if status == 201 and role == "admin":
        _, _, body = Client().request(BACKEND + "/admin/realms/master/users?" + urllib.parse.urlencode({"username": username, "exact": "true"}), headers=headers)
        _, _, definition = Client().request(BACKEND + "/admin/realms/master/roles/admin", headers=headers)
        status, _, _ = Client().request(BACKEND + "/admin/realms/master/users/" + json.loads(body)[0]["id"] + "/role-mappings/realm", "POST",
                                        b"[" + definition + b"]", headers)
        status = 201 if status == 204 else status
    print(status)


def delete_user(admin, password_file, username):
    headers = bootstrap_headers(password_file, admin)
    status, _, body = Client().request(BACKEND + "/admin/realms/master/users?" + urllib.parse.urlencode({"username": username, "exact": "true"}), headers=headers)
    found = json.loads(body) if status == 200 else []
    if len(found) != 1:
        raise SystemExit("probe: user not found")
    status, _, _ = Client().request(BACKEND + "/admin/realms/master/users/" + found[0]["id"], "DELETE", headers=headers)
    print(status)


if __name__ == "__main__":
    command, *rest = sys.argv[1:]
    {"signin": signin, "introspect": introspect, "token": token, "half-admin": half_admin, "delete-user": delete_user}[command](*rest)

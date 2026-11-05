"""Loopback-only break-glass test with identity stopped; no private output."""
import http.client
import http.cookies
import json
import sys
from pathlib import Path


def main():
    source = Path("/var/lib/terrace-documenso-inputs/secrets.json")
    assert source.stat().st_mode & 0o077 == 0 and source.stat().st_size <= 65536
    recovery = json.loads(source.read_text())["recoveryUser"]
    cookies = {}

    def request(method, path, value=None):
        connection = http.client.HTTPConnection("127.0.0.1", 13050, timeout=15)
        try:
            connection.request(method, path, None if value is None else json.dumps(value), {
                "Host": "documents.terrace.invalid", "Origin": "https://documents.terrace.invalid",
                "Content-Type": "application/json", "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
            })
            response = connection.getresponse()
            body = response.read(65537)
            assert len(body) <= 65536
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    parsed = http.cookies.SimpleCookie(value)
                    cookies.update({key: item.value for key, item in parsed.items()})
            return response.status, json.loads(body) if body else None
        finally:
            connection.close()

    status, csrf = request("GET", "/api/auth/csrf")
    assert status == 200
    status, _ = request("POST", "/api/auth/email-password/authorize", {
        "email": recovery["email"], "password": recovery["password"], "csrfToken": csrf["csrfToken"],
    })
    assert status == 201
    status, session = request("GET", "/api/auth/session")
    assert status == 200 and session["user"]["email"] == recovery["email"]
    assert session["user"]["roles"] == ["ADMIN"]


if __name__ == "__main__":
    try:
        main()
        print("Native loopback recovery administrator authenticated without identity.")
    except BaseException:
        print("Native recovery check failed; private diagnostics suppressed.", file=sys.stderr)
        sys.exit(1)

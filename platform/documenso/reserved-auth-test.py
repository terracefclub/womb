"""Test-only native HTTP denial; no credentials, tokens or response bodies emitted."""
import http.client
import http.cookies
import json
import sys


def main():
    cookies = {}

    def request(method, path, value=None):
        connection = http.client.HTTPConnection("127.0.0.1", 13050, timeout=10)
        try:
            connection.request(method, path, None if value is None else json.dumps(value), {
                "Host": "documents.terrace.invalid", "Origin": "https://documents.terrace.invalid",
                "Content-Type": "application/json", "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
            })
            response = connection.getresponse()
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError()
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    parsed = http.cookies.SimpleCookie(value)
                    cookies.update({key: item.value for key, item in parsed.items()})
            return response.status, body
        finally:
            connection.close()

    for address in ["serviceaccount@documents.terrace.invalid", "deleted-account@documents.terrace.invalid"]:
        status, body = request("GET", "/api/auth/csrf")
        if status != 200:
            raise ValueError()
        csrf = json.loads(body)["csrfToken"]
        status, _ = request("POST", "/api/auth/email-password/authorize", {
            "email": address, "password": "synthetic-denial-password-only", "csrfToken": csrf,
        })
        if status != 403 or any("sessionId" in name for name in cookies):
            raise ValueError()


if __name__ == "__main__":
    try:
        main()
        print("Reserved application identities denied native sign-in.")
    except Exception:
        print("Reserved identity authentication check failed; private diagnostics suppressed.", file=sys.stderr)
        sys.exit(1)

"""One real-browser regression for Origin on denied-account switch POST."""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

WEB = "https://web.terrace.invalid"
stage = "browser startup"
try:
    credentials = json.loads(Path("/root/gateway-fixture.json").read_text())
    users = {user["username"]: user for user in credentials["users"]}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ["TERRACE_BROWSER_BINARY"],
            headless=True, args=["--no-sandbox"],
        )
        try:
            context = browser.new_context(java_script_enabled=False)
            context.set_default_timeout(30000)
            page = context.new_page()
            origins = []

            def capture(request):
                if request.url == WEB + "/auth/login" and request.method == "POST":
                    origins.append(request.headers.get("origin"))

            page.on("request", capture)

            def enter(username):
                page.locator("#username").fill(username)
                page.locator("#password").fill(users[username]["password"])
                page.locator("#kc-login").click()

            stage = "unapproved account denial"
            page.goto(WEB + "/auth/login")
            enter("local-unapproved")
            page.get_by_role("heading", name="Approval comes next.", exact=True).wait_for()
            stage = "native switch-account POST origin"
            page.get_by_role("button", name="Use a different account", exact=True).click()
            page.locator("#username").wait_for()
            assert origins == [WEB]
            stage = "approved account after switch"
            enter("local-member")
            page.get_by_role("heading", name="Pull up a chair.", exact=True).wait_for()
            assert page.url == WEB + "/members/"
            assert any(cookie["name"] == "terrace_member" and cookie["secure"] and cookie["httpOnly"] for cookie in context.cookies())
            print("Real Chromium denied-to-switch-to-approved checks passed: HTTPS trust, native POST Origin, approved page and secure cookie.")
        finally:
            browser.close()
except BaseException:
    # Browser exceptions often contain callback URLs, cookie or password details.
    print("Native HTTPS browser check failed at " + stage + ".", file=sys.stderr)
    sys.exit(1)

#!/usr/bin/env python3
"""Unit tests for terrace-identity-admin against an in-memory stand-in of the Keycloak admin API.

No Keycloak, network or root is needed; the VM proof (platform/production-identity-check)
exercises the real server. Run: python3 platform/production/test_terrace_identity_admin.py -v
"""
import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("terrace_identity_admin", str(HERE / "terrace-identity-admin"))
spec = importlib.util.spec_from_loader("terrace_identity_admin", loader)
tool = importlib.util.module_from_spec(spec)
loader.exec_module(tool)

PROFILE = {"attributes": [
    {"name": "username", "permissions": {"view": ["admin", "user"], "edit": ["admin", "user"]}, "validations": {"length": {"min": 3, "max": 255}}},
    {"name": "email", "permissions": {"view": ["admin", "user"], "edit": ["admin"]}, "required": {"roles": ["user"]}},
    {"name": "lastName", "permissions": {"view": ["admin", "user"], "edit": ["admin", "user"]}},
], "groups": [{"name": "user-metadata", "displayHeader": "User metadata"}]}
MAPPER = {"name": "community-resource", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
          "config": {"included.custom.audience": "terrace-community", "id.token.claim": "false", "access.token.claim": "true"}}
DEFINITION = {
    "realm": "terrace", "enabled": True, "displayName": "Terrace", "registrationAllowed": False, "sslRequired": "all",
    "accessTokenLifespan": 60, "verifyEmail": True,
    "roles": {"realm": [{"name": n} for n in ("current-member", "alumni", "editorial-editor", "editorial-admin")]},
    "components": {tool.PROFILE: [{"providerId": "declarative-user-profile", "subComponents": {}, "config": {"kc.user.profile.config": [json.dumps(PROFILE)]}}]},
    "clients": [
        {"clientId": "terrace-web", "secret": "<SOPS:placeholder>", "enabled": True, "publicClient": False, "protocol": "openid-connect",
         "standardFlowEnabled": True, "directAccessGrantsEnabled": False, "implicitFlowEnabled": False, "fullScopeAllowed": False,
         "clientAuthenticatorType": "client-secret", "serviceAccountsEnabled": False,
         "redirectUris": ["https://web.example/auth/callback"], "webOrigins": ["https://web.example"],
         "attributes": {"pkce.code.challenge.method": "S256"}, "protocolMappers": [MAPPER],
         "defaultClientScopes": ["basic", "profile", "email", "roles"], "optionalClientScopes": []},
        {"clientId": "terrace-community", "secret": "<SOPS:placeholder2>", "enabled": True, "publicClient": False,
         "standardFlowEnabled": False, "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False, "redirectUris": [], "webOrigins": []},
    ],
    "scopeMappings": [{"client": "terrace-web", "roles": ["current-member", "alumni"]}],
}
CONFIG = {"realm": "terrace", "base": "http://127.0.0.1:8088", "clientSecrets": {"terrace-web": {"file": "/nonexistent", "consumers": []},
                                                                               "terrace-community": {"file": "/nonexistent", "consumers": []}}}


class FakeKeycloak:
    """The parts of the admin REST API the reconciler and the role commands use, as Keycloak stores them."""

    def __init__(self, definition=DEFINITION):
        d = copy.deepcopy(definition)
        self.realm = {k: v for k, v in d.items() if k in tool.REALM_SCALARS or k in ("realm", "enabled")}
        self.realm = {"adminEventsEnabled": False, "adminEventsDetailsEnabled": False} | self.realm  # Keycloak always reports both
        self.roles = [r["name"] for r in d["roles"]["realm"]] + ["offline_access", "uma_authorization", "default-roles-terrace"]
        profile = json.loads(d["components"][tool.PROFILE][0]["config"]["kc.user.profile.config"][0])
        for attribute in profile["attributes"]:  # Keycloak adds keys of its own
            attribute["annotations"] = {}
        profile["unmanagedAttributePolicy"] = "ENABLED"
        self.profile = profile
        self.scopes = {name: f"scope-{name}" for name in ("basic", "profile", "email", "roles", "acr", "web-origins")}
        self.clients = {}
        for index, client in enumerate(d["clients"]):
            rep = {k: v for k, v in client.items() if k not in ("defaultClientScopes", "optionalClientScopes")}
            rep["id"] = f"id-{index}"
            rep["attributes"] = {k: str(v) for k, v in (rep.get("attributes") or {}).items()} | {"backchannel.logout.session.required": "true"}
            rep.setdefault("protocolMappers", [])
            for m, mapper in enumerate(rep["protocolMappers"]):
                mapper["id"] = f"mapper-{index}-{m}"
                mapper["config"]["introspection.token.claim"] = "true"
            self.clients[client["clientId"]] = {"rep": rep, "default": list(client.get("defaultClientScopes", ["profile", "email"])),
                                                "optional": list(client.get("optionalClientScopes", [])), "mappings": []}
        for mapping in d["scopeMappings"]:
            self.clients[mapping["client"]]["mappings"] = list(mapping["roles"])
        self.clients["terrace-web"]["default"].append("acr")  # an undeclared scope, as if added by hand
        self.clients["account"] = {"rep": {"id": "id-account", "clientId": "account"}, "default": [], "optional": [], "mappings": []}
        self.users = {}
        self.groups = []
        self.composites = {}
        self.writes = []

    def by_id(self, client_id):
        return next(c for c in self.clients.values() if c["rep"]["id"] == client_id)

    def get(self, path):
        parts = path.split("?")[0].split("/")
        if path == "terrace":
            return copy.deepcopy(self.realm)
        if parts[1:] == ["roles"]:
            return [{"name": n, "composite": n in self.composites} for n in self.roles]
        if parts[1:] == ["groups"]:
            return list(self.groups)
        if parts[1] == "roles" and parts[3:] == ["composites", "realm"]:
            return [{"name": n} for n in self.composites.get(parts[2], [])]
        if parts[1:] == ["users", "profile"]:
            return copy.deepcopy(self.profile)
        if parts[1:] == ["client-scopes"]:
            return [{"name": n, "id": i} for n, i in self.scopes.items()]
        if parts[1] == "clients" and len(parts) == 2:
            query = path.split("clientId=")[1] if "clientId=" in path else None
            return [copy.deepcopy(c["rep"]) for c in self.clients.values() if query in (None, c["rep"]["clientId"])]
        if parts[1] == "clients" and len(parts) == 3:
            return copy.deepcopy(self.by_id(parts[2])["rep"])
        if parts[1] == "clients" and parts[3].endswith("-client-scopes"):
            kind = parts[3].split("-")[0]
            return [{"name": n, "id": self.scopes[n]} for n in self.by_id(parts[2])[kind]]
        if parts[1] == "clients" and parts[3:] == ["scope-mappings", "realm"]:
            return [{"name": n} for n in self.by_id(parts[2])["mappings"]]
        if parts[1] == "clients" and parts[3:] == ["protocol-mappers", "models"]:
            return copy.deepcopy(self.by_id(parts[2])["rep"]["protocolMappers"])
        if parts[1] == "roles" and len(parts) == 4 and parts[3] == "users":
            return [u for u in self.users.values() if parts[2] in u["roles"]]
        if parts[1] == "users" and len(parts) == 4:
            return [{"name": r} for r in self.users[parts[2]]["roles"]]
        if parts[1] == "users":
            name = path.split("username=")[1].split("&")[0]
            return [copy.deepcopy(u) for u in self.users.values() if u["username"] == name]
        raise AssertionError("unexpected GET " + path)

    def paged(self, path):
        return self.get(path)

    def client(self, realm, client_id):
        found = self.get(f"{realm}/clients?clientId={client_id}")
        return found[0] if found else None

    def role(self, realm, name):
        return {"name": name}

    def role_holders(self, realm, name):
        return self.get(f"{realm}/roles/{name}/users")

    def realm_roles(self, realm, user_id):
        return sorted(self.users[user_id]["roles"])

    def user(self, realm, username):
        found = self.get(f"{realm}/users?username={username}&exact=true")
        return found[0] if found else None

    def call(self, method, path, body=None, text=None, expect=None):
        self.writes.append((method, path))
        parts = path.split("/")
        if method == "PUT" and path == "terrace":
            self.realm.update({k: v for k, v in body.items() if k in self.realm or k in tool.REALM_SCALARS})
        elif method == "POST" and parts[1:] == ["roles"]:
            self.roles.append(body["name"])
        elif method == "PUT" and parts[1:] == ["users", "profile"]:
            self.profile = copy.deepcopy(body)
        elif method == "PUT" and parts[1] == "clients" and len(parts) == 3:
            self.by_id(parts[2])["rep"] = copy.deepcopy(body)
        elif parts[1] == "clients" and parts[3:] == ["scope-mappings", "realm"]:
            mappings = self.by_id(parts[2])["mappings"]
            for role in body:
                (mappings.append if method == "POST" else mappings.remove)(role["name"])
        elif method == "PUT" and parts[1] == "clients" and parts[3].endswith("-client-scopes"):
            name = next(n for n, i in self.scopes.items() if i == parts[4])
            self.by_id(parts[2])[parts[3].split("-")[0]].append(name)
        elif parts[1] == "users" and parts[3:] == ["role-mappings", "realm"]:
            roles = self.users[parts[2]]["roles"]
            for role in body:
                (roles.append if method == "POST" else roles.remove)(role["name"])
        elif parts[1] == "clients" and parts[3:5] == ["protocol-mappers", "models"]:
            mappers = self.by_id(parts[2])["rep"]["protocolMappers"]
            if method == "PUT":
                mappers[[m["id"] for m in mappers].index(parts[5])] = copy.deepcopy(body)
            else:
                mappers.append({**copy.deepcopy(body), "id": "mapper-new"})
        elif method == "POST" and parts[3:] == ["logout"]:
            pass
        elif method == "PUT" and parts[1] == "users":
            self.users[parts[2]].update(enabled=body["enabled"])
        else:
            raise AssertionError(f"unexpected {method} {path}")


def plan_for(definition, api=None):
    api = api or FakeKeycloak()
    return tool.realm_plan(api, CONFIG, definition), api


def changed(**edits):
    d = copy.deepcopy(DEFINITION)
    for function in edits.values():
        function(d)
    return d


class ReconcileTest(unittest.TestCase):
    def test_imported_realm_is_converged_despite_keycloak_additions(self):
        plan, _ = plan_for(DEFINITION)
        self.assertEqual((plan.changes, plan.refused), ([], []))
        self.assertIn("default scope acr on client terrace-web (not declared)", plan.kept)

    def test_declared_differences_are_planned_applied_and_then_converged(self):
        def web(d):
            return d["clients"][0]
        d = changed(
            lifespan=lambda d: d.update(accessTokenLifespan=120),
            role=lambda d: d["roles"]["realm"].append({"name": "probe"}),
            redirect=lambda d: web(d)["redirectUris"].append("https://web.example/v2"),
            attribute=lambda d: web(d)["attributes"].update({"post.logout.redirect.uris": "https://web.example/"}),
            mapper=lambda d: web(d)["protocolMappers"][0]["config"].update({"access.token.claim": "false"}),
            scope=lambda d: d["scopeMappings"][0]["roles"].remove("alumni"),
            profile=lambda d: d["components"][tool.PROFILE][0]["config"].update({"kc.user.profile.config": [json.dumps({**PROFILE, "attributes": PROFILE["attributes"] + [{"name": "pronouns"}]})]}),
        )
        plan, api = plan_for(d)
        self.assertEqual(plan.refused, [])
        self.assertEqual(sorted(c["kind"] for c in plan.changes),
                         ["client-update", "mapper-update", "realm-setting", "role-create", "scope-mapping-remove", "user-profile"])
        first = plan.digest()
        self.assertEqual(first, tool.realm_plan(api, CONFIG, d).digest(), "the plan hash is stable")
        for change in plan.changes:
            tool.apply_change(api, CONFIG, d, change)
        after = tool.realm_plan(api, CONFIG, d)
        self.assertEqual((after.changes, after.refused), ([], []))
        self.assertIn("unmanagedAttributePolicy", api.profile)
        self.assertEqual(api.clients["terrace-web"]["rep"]["secret"], "<SOPS:placeholder>", "the secret field is never planned or changed")
        self.assertTrue(all(method != "DELETE" or "scope-mappings" in path for method, path in api.writes))

    def test_plan_shows_mapper_and_profile_content_and_the_hash_binds_it(self):
        mapper = {"name": "extra", "protocol": "openid-connect", "protocolMapper": "oidc-hardcoded-role-mapper", "config": {"role": "editorial-admin"}}
        d = changed(mapper=lambda d: d["clients"][0]["protocolMappers"].append(mapper))
        plan, _ = plan_for(d)
        text = "\n".join(tool.describe(c) for c in plan.changes)
        self.assertIn("type oidc-hardcoded-role-mapper", text)
        self.assertIn('"role": "editorial-admin"', text)

        def profile(edit):
            data = copy.deepcopy(PROFILE)
            edit(data)
            return lambda d: d["components"][tool.PROFILE][0]["config"].update({"kc.user.profile.config": [json.dumps(data)]})
        one, _ = plan_for(changed(p=profile(lambda x: x["attributes"][2]["permissions"].update(edit=["admin"]))))
        two, _ = plan_for(changed(p=profile(lambda x: x["attributes"][2]["permissions"].update(edit=["admin", "user", "x"]))))
        self.assertEqual([c["kind"] for c in one.changes], ["user-profile"])
        self.assertEqual(one.changes[0]["attributes"], two.changes[0]["attributes"], "same attribute names")
        self.assertNotEqual(one.digest(), two.digest(), "the hash binds the declared content, not only the names")
        self.assertIn('"edit": ["admin"]', tool.describe(one.changes[0]))
        api = FakeKeycloak()
        tool.apply_change(api, CONFIG, {}, one.changes[0])  # apply uses the plan's content, not a re-read definition
        self.assertEqual([a for a in api.profile["attributes"] if a["name"] == "lastName"][0]["permissions"]["edit"], ["admin"])

    def test_never_deletes_and_reports_what_it_keeps(self):
        api = FakeKeycloak()
        api.roles.append("operator-added")
        api.clients["extra"] = {"rep": {"id": "id-x", "clientId": "extra"}, "default": [], "optional": [], "mappings": []}
        plan, _ = plan_for(DEFINITION, api)
        self.assertEqual(plan.changes, [])
        self.assertTrue(any("operator-added" in k for k in plan.kept) and any("client extra" in k for k in plan.kept))

    def test_destructive_or_unknown_changes_are_refused(self):
        cases = {
            "drop": lambda d: d["components"][tool.PROFILE][0]["config"].update({"kc.user.profile.config": [json.dumps({**PROFILE, "attributes": PROFILE["attributes"][:2]})]}),
            "public": lambda d: d["clients"][0].update(publicClient=True),
            "unknown": lambda d: d.update(someFutureSetting=True),
            "unknown near a managed key": lambda d: d.update(adminEventsExtra=True),
            "client key": lambda d: d["clients"][0].update(authorizationSettings={}),
            "disable": lambda d: d["clients"][0].update(enabled=False),
            "realm off": lambda d: d.update(enabled=False),
            "new client without secret": lambda d: d["clients"].append({"clientId": "terrace-new", "redirectUris": []}),
            "client roles": lambda d: d["roles"].update(client={}),
        }
        for label, edit in cases.items():
            with self.subTest(label):
                plan, _ = plan_for(changed(edit=edit))
                self.assertTrue(plan.refused, label)

    def test_admin_events_keys_absent_are_not_managed(self):
        for running in ({}, {"adminEventsEnabled": True}, {"adminEventsEnabled": True, "adminEventsDetailsEnabled": True}):
            api = FakeKeycloak()
            api.realm.update(running)
            plan, _ = plan_for(DEFINITION, api)
            self.assertEqual((plan.changes, plan.refused), ([], []), running)

    def test_admin_events_keys_drifting_are_planned_applied_and_converged(self):
        d = changed(events=lambda d: d.update(adminEventsEnabled=True, adminEventsDetailsEnabled=False))
        plan, api = plan_for(d)
        self.assertEqual(plan.refused, [])
        self.assertEqual([(c["kind"], c["target"]) for c in plan.changes], [("realm-setting", "terrace/adminEventsEnabled")])
        self.assertEqual(tool.describe(plan.changes[0]), "~ realm adminEventsEnabled: false -> true")
        tool.apply_change(api, CONFIG, d, plan.changes[0])
        self.assertEqual((api.realm["adminEventsEnabled"], api.realm["adminEventsDetailsEnabled"]), (True, False))
        self.assertEqual(plan_for(d, api)[0].changes, [])
        # Declared false while running true: drift too (the artifact is the source of truth).
        api.realm["adminEventsEnabled"] = False
        api.realm["adminEventsDetailsEnabled"] = True
        plan, _ = plan_for(changed(events=lambda d: d.update(adminEventsEnabled=False, adminEventsDetailsEnabled=False)), api)
        self.assertEqual(sorted(c["target"] for c in plan.changes), ["terrace/adminEventsDetailsEnabled"])

    def test_admin_events_keys_already_equal_are_converged(self):
        d = changed(events=lambda d: d.update(adminEventsEnabled=True, adminEventsDetailsEnabled=False))
        api = FakeKeycloak(d)
        plan, _ = plan_for(d, api)
        self.assertEqual((plan.changes, plan.refused), ([], []))

    def test_admin_events_keys_must_be_booleans(self):
        for key in ("adminEventsEnabled", "adminEventsDetailsEnabled"):
            for value in ("true", "false", 1, 0, None, [], {}):
                with self.subTest(key=key, value=value):
                    plan, _ = plan_for(changed(bad=lambda d: d.update({key: value})))
                    self.assertTrue(any(key in item for item in plan.refused), plan.refused)
                    self.assertEqual(plan.changes, [])

    def test_admin_events_details_true_is_refused_and_never_applied(self):
        plan, api = plan_for(changed(details=lambda d: d.update(adminEventsEnabled=True, adminEventsDetailsEnabled=True)))
        self.assertTrue(any("personal data" in item for item in plan.refused))
        self.assertFalse(any(c["target"].endswith("adminEventsDetailsEnabled") for c in plan.changes))
        self.assertEqual(api.writes, [])

    def test_new_client_with_a_delivered_secret_is_created_without_its_secret_in_the_plan(self):
        config = copy.deepcopy(CONFIG)
        config["clientSecrets"]["terrace-new"] = {"file": "/nonexistent", "consumers": []}
        d = changed(new=lambda d: d["clients"].append({"clientId": "terrace-new", "secret": "<SOPS:x>", "redirectUris": []}))
        plan = tool.realm_plan(FakeKeycloak(), config, d)
        self.assertEqual([c["kind"] for c in plan.changes], ["client-create"])
        self.assertNotIn("secret", plan.changes[0]["definition"])


class RolesTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeKeycloak()
        self.api.users = {"u1": {"id": "u1", "username": "ad-one", "enabled": True, "roles": ["editorial-admin"]},
                          "u2": {"id": "u2", "username": "ed-two", "enabled": True, "roles": ["editorial-editor"]}}
        self.config = {**CONFIG, "definition": str(Path(self.tmp().name) / "def.json")}
        Path(self.config["definition"]).write_text(json.dumps(DEFINITION))
        self.patches = [mock.patch.object(tool, "login", lambda config, args, **_: self.api), mock.patch.object(tool, "audit", self.record)]
        self.audits = []
        for patch in self.patches:
            patch.start()

    def tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return self._tmp

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def record(self, action, target, result, admin="-"):
        self.audits.append((action, target, result))

    def run_cmd(self, *argv):
        args = tool.parser().parse_args(list(argv) + ["--admin", "ops-a"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                tool.cmd_user(self.config, args)
            except tool.Refused as error:
                return "REFUSED " + str(error)
        return out.getvalue()

    def test_last_site_administrator_cannot_lose_the_role_or_be_disabled(self):
        self.assertIn("last enabled editorial-admin", self.run_cmd("revoke", "--username", "ad-one", "--role", "editorial-admin"))
        self.assertIn("last enabled editorial-admin", self.run_cmd("user", "disable", "--username", "ad-one"))
        self.assertIn(("user-revoke", "terrace/user/ad-one/role/editorial-admin", "refused-last-admin"), self.audits)
        self.run_cmd("grant", "--username", "ed-two", "--role", "editorial-admin")
        self.assertIn("Revoke done", self.run_cmd("revoke", "--username", "ad-one", "--role", "editorial-admin"))
        self.assertEqual(self.api.users["u1"]["roles"], [])

    def test_grant_is_idempotent_dry_run_changes_nothing_and_roles_come_from_the_contract(self):
        self.assertIn("would grant", self.run_cmd("grant", "--username", "ed-two", "--role", "editorial-admin", "--dry-run"))
        self.assertEqual(self.api.users["u2"]["roles"], ["editorial-editor"])
        self.assertIn("already has", self.run_cmd("grant", "--username", "ed-two", "--role", "editorial-editor"))
        self.assertIn("not a role of the identity contract", self.run_cmd("grant", "--username", "ed-two", "--role", "admin"))
        for action in ("grant", "revoke"):
            self.assertIn("member role", self.run_cmd(action, "--username", "ed-two", "--role", "alumni"))
        self.assertEqual(self.api.users["u2"]["roles"], ["editorial-editor"])

    def test_last_administrator_check_refuses_when_the_role_can_be_inherited(self):
        self.api.groups = [{"name": "staff"}]
        self.assertIn("cannot count editorial-admin holders reliably", self.run_cmd("revoke", "--username", "ad-one", "--role", "editorial-admin"))
        self.api.groups = []
        self.api.roles.append("super-editor")
        self.api.composites = {"super-editor": ["editorial-admin"]}
        self.assertIn("composite role super-editor", self.run_cmd("user", "disable", "--username", "ad-one"))
        self.assertIn(("user-disable", "terrace/user/ad-one", "refused-inherited-role"), self.audits)
        self.assertEqual(self.api.users["u1"]["roles"], ["editorial-admin"])
        self.assertIn("member accounts follow", self.run_cmd("user", "create", "--username", "mem-x", "--email", "m@x.example",
                                                             "--first-name", "M", "--last-name", "X", "--role", "alumni"))


class FakeMaster:
    """Master-realm users with passwords, labels and roles, as first-admin and break-glass see them."""

    def __init__(self):
        self.users = {}
        self.realm = {"realm": "master", "bruteForceProtected": False}
        self.events = {"adminEventsEnabled": False}

    def hardening(self):
        return self.realm.get("bruteForceProtected") is True, self.events.get("adminEventsEnabled") is True

    def add(self, name, password=None, roles=(), enabled=True):
        self.users[name] = {"id": "id-" + name, "username": name, "enabled": enabled, "password": password, "label": None, "roles": list(roles)}

    def by_id(self, user_id):
        return next(u for u in self.users.values() if u["id"] == user_id)

    def grant(self, base, username, password):
        user = self.users.get(username)
        return bool(user and user["enabled"] and password and user["password"] == password)

    def api(self, base, admin, password):
        if not self.grant(base, admin, password):
            raise tool.Refused(f"authentication as {admin} failed")
        fake = self

        class Api:
            def user(self, realm, name):
                user = fake.users.get(name)
                return None if user is None else {k: v for k, v in user.items() if k in ("id", "username", "enabled")}

            def role(self, realm, name):
                return {"name": name}

            def realm_roles(self, realm, user_id):
                return list(fake.by_id(user_id)["roles"])

            def get(self, path):
                parts = path.split("/")
                if parts[-1] == "credentials":
                    user = fake.by_id(parts[2])
                    return [{"id": "c", "type": "password", "userLabel": user["label"]}] if user["password"] else []
                if path == "master":
                    return dict(fake.realm)
                if path == "master/events/config":
                    return dict(fake.events)
                raise AssertionError(path)

            def paged(self, path):
                return [{"id": u["id"], "username": u["username"]} for u in fake.users.values()]

            def call(self, method, path, body=None, text=None, expect=None):
                parts = path.split("/")
                if method == "POST" and path == "master/users":
                    fake.add(body["username"])
                elif parts[-1] == "reset-password":
                    fake.by_id(parts[2])["password"] = body["value"]
                elif parts[-1] == "userLabel":
                    fake.by_id(parts[2])["label"] = text
                elif parts[-1] == "realm" and method == "POST":
                    fake.by_id(parts[2])["roles"] += [r["name"] for r in body]
                elif method == "DELETE" and parts[1] == "users":
                    del fake.users[fake.by_id(parts[2])["username"]]
                elif method == "PUT" and path == "master":
                    fake.realm = dict(body)
                elif method == "PUT" and path == "master/events/config":
                    fake.events = dict(body)
                elif method == "PUT" and parts[1] == "users":
                    fake.by_id(parts[2])["enabled"] = body["enabled"]
                else:
                    raise AssertionError(method + " " + path)
        return Api()


class FirstAdminAndBreakGlassTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        run = Path(self._tmp.name)
        self.master = FakeMaster()
        self.files = {}
        self.order = []
        self.config = {"base": "http://127.0.0.1:8088", "management": "http://127.0.0.1:9000"}
        patches = [
            mock.patch.object(tool, "DELIVERED", run / "bootstrap-admin-password"),
            mock.patch.object(tool, "HANDOFF", run / "handoff"),
            mock.patch.object(tool, "master_users", lambda: [(u["username"], u["enabled"]) for u in self.master.users.values()]),
            mock.patch.object(tool, "read_private", lambda path, pattern=None: self.files.get(str(path))),
            mock.patch.object(tool, "write_private", self.write),
            mock.patch.object(tool, "remove", lambda path: self.files.pop(str(path), None)),
            mock.patch.object(tool, "run_bootstrap", self.bootstrap),
            mock.patch.object(tool, "password_grant", self.master.grant),
            mock.patch.object(tool, "Keycloak", self.master.api),
            mock.patch.object(tool, "audit", lambda *a, **k: None),
            mock.patch.object(tool, "master_hardening", self.master.hardening),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.files[str(tool.DELIVERED)] = "b" * 64

    def write(self, path, value):
        self.order.append(("write", str(path)))
        self.files[str(path)] = value.strip()

    def bootstrap(self, config, username, password):
        self.order.append(("bootstrap", username))
        self.master.add(username, password, roles=["admin"])

    def first_admin(self, *extra):
        args = tool.parser().parse_args(["first-admin", "--username", "ops-a", "--email", "a@x.example", "--first-name", "A", "--last-name", "B", *extra])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tool.cmd_first_admin(self.config, args)
        return out.getvalue()

    def test_first_admin_creates_proves_and_removes_the_bootstrap_account(self):
        self.assertEqual(self.master.hardening(), (False, False))
        self.assertIn("ready", self.first_admin())
        # The success path itself turns the master realm's hardening on (not only a later login).
        self.assertEqual(self.master.hardening(), (True, True))
        self.assertEqual(self.master.realm["failureFactor"], 5)
        self.assertEqual(sorted(self.master.users), ["ops-a"])
        admin = self.master.users["ops-a"]
        self.assertEqual((admin["roles"], admin["label"]), (["admin"], tool.INITIAL_LABEL))
        handoff = str(tool.handoff_path("master", "ops-a"))
        self.assertEqual(self.files[handoff], admin["password"])
        self.assertNotIn(str(tool.DELIVERED), self.files)
        self.assertIn("Already done", self.first_admin())

    def test_resume_repairs_a_half_created_administrator_before_removing_the_bootstrap_account(self):
        # Died after the POST: the bootstrap account exists, the named user has no password and no role.
        self.master.add(tool.BOOTSTRAP_USER, "b" * 64, roles=["admin"])
        self.master.add("ops-a")
        self.first_admin()
        admin = self.master.users["ops-a"]
        self.assertEqual(admin["roles"], ["admin"])
        self.assertEqual(self.files[str(tool.handoff_path("master", "ops-a"))], admin["password"])
        self.assertNotIn(tool.BOOTSTRAP_USER, self.master.users)
        handoff = ("write", str(tool.handoff_path("master", "ops-a")))
        self.assertIn(handoff, self.order, "the generated password is written to its handoff file")

    def test_bootstrap_account_kept_when_the_named_administrator_cannot_be_proven(self):
        self.master.add(tool.BOOTSTRAP_USER, "b" * 64, roles=["admin"])
        self.master.add("ops-a", enabled=True)
        real_grant = self.master.grant
        with mock.patch.object(tool, "password_grant", lambda base, name, pw: False if name == "ops-a" else real_grant(base, name, pw)):
            with self.assertRaises(tool.Refused):
                self.first_admin()
        self.assertIn(tool.BOOTSTRAP_USER, self.master.users)
        self.assertIn(str(tool.DELIVERED), self.files, "the delivered password stays for the next attempt")

    def test_resume_never_touches_an_account_with_a_password_of_its_own(self):
        # Run 1 created ops-a and died before removing the bootstrap account; ops-a then chose a password.
        self.master.add(tool.BOOTSTRAP_USER, "b" * 64, roles=["admin"])
        self.master.add("ops-a", "chosen-password-1", enabled=False)
        with self.assertRaises(tool.Refused) as refused:
            self.first_admin()
        self.assertIn("already has a password of its own", str(refused.exception))
        admin = self.master.users["ops-a"]
        self.assertEqual((admin["password"], admin["enabled"], admin["roles"]), ("chosen-password-1", False, []), "not reset, enabled or granted")
        self.assertIn(tool.BOOTSTRAP_USER, self.master.users)
        # Still carrying the tool's initial label: that one is repaired.
        admin.update(label=tool.INITIAL_LABEL)
        self.assertIn("ready", self.first_admin())
        self.assertEqual(self.master.users["ops-a"]["roles"], ["admin"])

    def test_hardening_converges_after_a_refused_repair_and_break_glass_end(self):
        # Run 1 created ops-a (admin) and died before hardening and removing the bootstrap account; ops-a chose a password.
        self.master.add(tool.BOOTSTRAP_USER, "b" * 64, roles=["admin"])
        self.master.add("ops-a", "chosen-password-1", roles=["admin"])
        with self.assertRaises(tool.Refused):
            self.first_admin()
        self.assertEqual(self.master.hardening(), (False, False))
        # The documented remedy, as a state-changing command: hardening converges at the administrator's login.
        args = tool.parser().parse_args(["break-glass", "end", "--admin", "ops-a", "--password-stdin"])
        out = io.StringIO()
        with mock.patch.object(tool, "HARDEN", [True]), mock.patch.object(tool, "read_passwords", lambda a, p: ["chosen-password-1"]), \
                contextlib.redirect_stdout(out):
            tool.cmd_break_glass(self.config, args)
        self.assertIn("turned on: brute-force protection, admin events", out.getvalue())
        self.assertEqual(self.master.hardening(), (True, True))
        self.assertEqual(self.master.realm["failureFactor"], 5)
        self.assertNotIn(tool.BOOTSTRAP_USER, self.master.users)
        text = self.first_admin()
        self.assertIn("Already done", text)
        self.assertNotIn("WARNING", text)
        # Idempotent: a second login changes nothing and says nothing.
        out = io.StringIO()
        with mock.patch.object(tool, "HARDEN", [True]), mock.patch.object(tool, "read_passwords", lambda a, p: ["chosen-password-1"]), \
                contextlib.redirect_stdout(out):
            tool.login(self.config, args)
        self.assertEqual(out.getvalue(), "")

    def test_a_dry_run_reports_missing_hardening_and_changes_nothing(self):
        self.master.add("ops-a", "chosen-password-1", roles=["admin"])
        self.assertEqual(self.master.hardening(), (False, False))
        args = tool.parser().parse_args(["admin", "disable", "--admin", "ops-a", "--username", "ops-b", "--password-stdin", "--dry-run"])
        self.assertTrue(tool.mutating(args))
        out = io.StringIO()
        with mock.patch.object(tool, "HARDEN", [True]), mock.patch.object(tool, "read_passwords", lambda a, p: ["chosen-password-1"]), \
                contextlib.redirect_stdout(out):
            tool.login(self.config, args)
        self.assertEqual(self.master.hardening(), (False, False))
        self.assertIn("without --dry-run this would turn on: brute-force protection, admin events", out.getvalue())
        # The same login without --dry-run turns it on.
        args = tool.parser().parse_args(["admin", "disable", "--admin", "ops-a", "--username", "ops-b", "--password-stdin"])
        with mock.patch.object(tool, "HARDEN", [True]), mock.patch.object(tool, "read_passwords", lambda a, p: ["chosen-password-1"]), \
                contextlib.redirect_stdout(io.StringIO()):
            tool.login(self.config, args)
        self.assertEqual(self.master.hardening(), (True, True))

    def test_already_done_warns_when_hardening_is_incomplete(self):
        self.master.add("ops-a", "x" * 20, roles=["admin"])
        self.assertIn("WARNING: master realm hardening is incomplete", self.first_admin())

    def test_already_done_removes_a_left_over_delivered_file(self):
        self.master.add("ops-a", "x" * 20, roles=["admin"])
        self.assertIn("Already done", self.first_admin())
        self.assertNotIn(str(tool.DELIVERED), self.files)

    def begin(self, dry_run=False):
        args = tool.parser().parse_args(["break-glass", "begin"] + (["--dry-run"] if dry_run else []))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tool.cmd_break_glass(self.config, args)
        return out.getvalue()

    def test_break_glass_writes_the_handoff_before_the_bootstrap_command(self):
        self.begin()
        kinds = [kind for kind, _ in self.order]
        self.assertEqual(kinds, ["write", "bootstrap"])
        recovery = [n for n in self.master.users if n.startswith(tool.RECOVERY_PREFIX)][0]
        self.assertEqual(self.files[str(tool.handoff_path("master", recovery))], self.master.users[recovery]["password"])

    def test_recovery_names_are_not_guessable_and_a_failed_begin_leaves_no_useless_handoff(self):
        self.begin()
        recovery = [n for n in self.master.users if n.startswith(tool.RECOVERY_PREFIX)][0]
        self.assertRegex(recovery, r"^terrace-recovery-[0-9]{14}-[0-9a-f]{8}$")

        def failing(config, username, password):
            raise tool.Refused("bootstrap did not succeed")
        self.master.users.clear()
        self.files = {k: v for k, v in self.files.items() if "/handoff/" not in k}
        with mock.patch.object(tool, "run_bootstrap", failing), self.assertRaises(tool.Refused):
            self.begin()
        self.assertEqual([p for p in self.files if "/handoff/" in p], [], "no handoff for an account that was never created")

    def test_break_glass_proceeds_past_unusable_temporary_accounts_and_refuses_a_working_one(self):
        # A bootstrap account nobody can use (delivered file gone) and a recovery account without a handoff.
        self.master.add(tool.BOOTSTRAP_USER, "b" * 64, roles=["admin"])
        self.master.add(tool.RECOVERY_PREFIX + "20260101000000", "r" * 64, roles=["admin"])
        self.files.pop(str(tool.DELIVERED))
        with mock.patch.object(tool.time, "strftime", lambda *a: "20261119000000"):
            self.assertIn("Unusable temporary administrators also present", self.begin())
            with self.assertRaises(tool.Refused) as refused:
                self.begin()
        self.assertIn("a working temporary administrator exists", str(refused.exception))


class CommandLineTest(unittest.TestCase):
    def test_one_state_changing_command_at_a_time(self):
        with tempfile.TemporaryDirectory() as run, mock.patch.object(tool, "RUN", Path(run)), mock.patch.object(tool, "LOCK", Path(run) / "lock"), \
                mock.patch.object(tool, "memory_filesystem", lambda path: True), mock.patch.object(os, "chown", lambda *a: None):
            held = tool.take_lock()
            with self.assertRaises(tool.Refused) as refused:
                tool.take_lock()
            self.assertIn("another terrace-identity-admin command is running", str(refused.exception))
            os.close(held)
            os.close(tool.take_lock())
        parse = tool.parser().parse_args
        self.assertTrue(tool.mutating(parse(["break-glass", "begin", "--dry-run"])))
        self.assertTrue(tool.mutating(parse(["realm", "apply", "--admin", "a-b", "--plan-sha256", "0" * 64])))
        self.assertFalse(tool.mutating(parse(["realm", "plan", "--admin", "a-b"])))
        self.assertFalse(tool.mutating(parse(["list", "--admin", "a-b"])))

    def test_unit_outcome_is_the_runs_own_recorded_result(self):
        with tempfile.TemporaryDirectory() as run:
            boot = Path(run)
            state = {"outcomes": iter([None, "stale success", "NONCE success"]), "busy": True, "starts": 0}

            def systemctl(argv, **_kwargs):
                if argv[1] == "start":
                    state["starts"] += 1
                    return mock.Mock(returncode=129)  # the systemctl client was killed by a hangup; the job runs on
                return mock.Mock(returncode=0, stdout=(b"Job=7\nActiveState=activating\n" if state["busy"] else b"Job=\nActiveState=inactive\n"))

            def tick(_seconds):
                value = next(state["outcomes"], None)
                if value is not None:
                    (boot / "result").write_text(value.replace("NONCE", (boot / "nonce").read_text()) + "\n")
            with mock.patch.object(tool, "KC_BOOT", boot), mock.patch.object(tool, "write_private", lambda p, v: Path(p).write_text(v)), \
                    mock.patch.object(tool.subprocess, "run", systemctl), mock.patch.object(tool.time, "sleep", tick):
                self.assertEqual(tool.run_unit("u.service"), 0)
                self.assertEqual(sorted(p.name for p in boot.iterdir()), [], "nonce and result removed")
                state["outcomes"] = iter(["NONCE exit-code"])
                self.assertEqual(tool.run_unit("u.service"), 1)
                # N2: a unit that never starts writes no result; once it is idle the tool gives up quickly.
                state.update(outcomes=iter([]), busy=False)
                with mock.patch.object(tool.time, "monotonic", iter(range(0, 10000, 1)).__next__):
                    self.assertEqual(tool.run_unit("u.service"), 1)

    def test_a_failed_bootstrap_restarts_keycloak_and_says_so(self):
        calls = []
        with mock.patch.object(tool, "private_dir", lambda p: p), mock.patch.object(tool, "write_private", lambda p, v: None), \
                mock.patch.object(tool, "remove", lambda p: None), mock.patch.object(tool, "run_unit", lambda unit: 1), \
                mock.patch.object(tool, "systemctl", lambda *argv, **k: calls.append(argv) or 0), \
                mock.patch.object(tool, "keycloak_ready", lambda config: True):
            with self.assertRaises(tool.Refused) as refused:
                tool.run_bootstrap({"bootstrapUnit": "b.service"}, "terrace-recovery-x", "p" * 64)
        self.assertIn(("start", "--no-block", "keycloak.service"), calls)
        self.assertIn(("reset-failed", "keycloak.service"), calls)
        self.assertIn("did not succeed; Keycloak is running and ready again", str(refused.exception))

    def test_realm_apply_never_takes_another_definition(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            tool.parser().parse_args(["realm", "apply", "--admin", "a-b", "--plan-sha256", "0" * 64, "--definition", "/tmp/x"])

    def test_old_temporary_administrators_fail_the_check_and_log_crit(self):
        now = 1_800_000_000
        rows = f"terrace-recovery-1|{(now - 3600) * 1000}\nterrace-bootstrap|{(now - 60) * 1000}\n".encode()
        logged = []
        hardened = [True, True]
        with mock.patch.object(tool.subprocess, "run", lambda *a, **k: mock.Mock(returncode=0, stdout=rows)), \
                mock.patch.object(tool, "master_users", lambda: [("ops-a", True)]), mock.patch.object(tool, "master_hardening", lambda: tuple(hardened)), \
                mock.patch.object(tool.time, "time", lambda: now), mock.patch.object(tool.syslog, "syslog", lambda prio, msg: logged.append((prio, msg))), \
                contextlib.redirect_stdout(io.StringIO()):
            args = tool.parser().parse_args(["check-temporary"])
            self.assertEqual(tool.cmd_check_temporary({"temporaryMaxAgeMinutes": 30}, args), tool.EXIT_REFUSED)
            self.assertEqual(logged[0][0], tool.syslog.LOG_CRIT)
            self.assertIn("terrace-recovery-1", logged[0][1])
            self.assertNotIn("terrace-bootstrap", logged[0][1])
            self.assertEqual(tool.cmd_check_temporary({"temporaryMaxAgeMinutes": 120}, args), 0)
            # A named administrator exists but the master realm is not hardened: the check fails too.
            hardened[0] = False
            logged.clear()
            self.assertEqual(tool.cmd_check_temporary({"temporaryMaxAgeMinutes": 120}, args), tool.EXIT_REFUSED)
            self.assertIn("brute-force protection or admin events are off", logged[0][1])

    def test_a_verification_grant_ends_its_session(self):
        calls = []

        def fake_http(url, method="GET", body=None, headers=None, timeout=30):
            calls.append(url.rsplit("/", 1)[-1])
            return (200, b'{"access_token": "a", "refresh_token": "r"}') if url.endswith("/token") else (204, b"")
        with mock.patch.object(tool, "http", fake_http):
            self.assertTrue(tool.password_grant("http://127.0.0.1:8088", "ops-a", "p" * 12))
        self.assertEqual(calls, ["token", "logout"])

    def test_no_terminal_input_is_a_refusal_not_a_traceback(self):
        args = tool.parser().parse_args(["list", "--admin", "ops-a"])
        tty = io.StringIO()
        tty.isatty = lambda: True
        for error in (EOFError, KeyboardInterrupt):
            with mock.patch.object(sys, "stdin", tty), mock.patch.object(tool.getpass, "getpass", mock.Mock(side_effect=error)):
                with self.assertRaises(tool.Refused) as refused:
                    tool.read_passwords(args, ["Password: "])
            self.assertIn("no password given", str(refused.exception))

    def test_stale_bootstrap_credential_files_are_removed_unless_the_unit_runs(self):
        with tempfile.TemporaryDirectory() as run:
            boot = Path(run)
            for name in ("username", "password", "nonce", "result"):
                (boot / name).write_text("x")
            with mock.patch.object(tool, "KC_BOOT", boot), mock.patch.object(tool, "unit_busy", lambda unit: True):
                tool.clean_stale_bootstrap_files({"bootstrapUnit": "b.service"})
                self.assertEqual(len(list(boot.iterdir())), 4, "kept while the unit runs")
            with mock.patch.object(tool, "KC_BOOT", boot), mock.patch.object(tool, "unit_busy", lambda unit: False):
                tool.clean_stale_bootstrap_files({"bootstrapUnit": "b.service"})
            self.assertEqual(sorted(p.name for p in boot.iterdir()), ["result"])

    def test_refusals_are_specific_oauth_errors(self):
        answers = iter([(401, b'{"error":"invalid_grant"}'), (400, b'{"error":"invalid_grant"}'), (500, b"oops"), (400, b'{"error":"invalid_scope"}'), (200, b"{}")])
        with mock.patch.object(tool, "http", lambda *a, **k: next(answers)):
            self.assertFalse(tool.password_grant("b", "u", "p" * 12))
            self.assertFalse(tool.password_grant("b", "u", "p" * 12))
            with self.assertRaises(tool.Refused):
                tool.password_grant("b", "u", "p" * 12)
            with self.assertRaises(tool.Refused):
                tool.password_grant("b", "u", "p" * 12)
            self.assertTrue(tool.password_grant("b", "u", "p" * 12))

    def call(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_refuses_non_root(self):
        with mock.patch.object(os, "geteuid", lambda: 1000):
            code, _, err = self.call(["status"])
        self.assertEqual(code, 1)
        self.assertIn("run as root", err)

    def test_plan_hash_and_client_id_are_validated(self):
        with mock.patch.object(os, "geteuid", lambda: 0):
            self.assertIn("64 lowercase hex", self.call(["realm", "apply", "--admin", "ops-a", "--plan-sha256", "abc"])[2])
            self.assertIn("client id", self.call(["client-secret", "sync", "--admin", "ops-a", "--client", "Bad;x"])[2])

    def test_stdin_passwords_are_consumed_line_by_line_and_validated(self):
        tool._STDIN.clear()
        args = tool.parser().parse_args(["password", "--admin", "ops-a", "--password-stdin"])
        with mock.patch.object(sys, "stdin", io.StringIO("current-password-1\nnew-password-long-enough\n")):
            self.assertEqual(tool.read_passwords(args, ["a"]), ["current-password-1"])
            self.assertEqual(tool.read_passwords(args, ["b"]), ["new-password-long-enough"])
            with self.assertRaises(tool.Refused):
                tool.read_passwords(args, ["c"])
        tool._STDIN.clear()
        with mock.patch.object(sys, "stdin", io.StringIO("short\n")), self.assertRaises(tool.Refused):
            tool.read_passwords(args, ["a"])
        tool._STDIN.clear()

    def test_audit_fields_cannot_inject_or_carry_spaces(self):
        self.assertEqual(tool.field("a b\nresult=done"), "a_b_result_done")
        self.assertTrue(tool.reserved("terrace-bootstrap") and tool.reserved("terrace-recovery-20261119000000") and not tool.reserved("ops-a"))

    def test_errors_never_echo_exception_text(self):
        def boom(config, args):
            raise ValueError("secret-value-in-message")
        with mock.patch.object(os, "geteuid", lambda: 0), mock.patch.object(tool, "load_config", lambda path: {}), \
                mock.patch.object(tool, "cmd_status", boom):
            args_parser = tool.parser()
            with mock.patch.object(tool, "parser", lambda: args_parser):
                args_parser._subparsers._group_actions[0].choices["status"].set_defaults(func=boom)
                code, out, err = self.call(["status"])
        self.assertEqual(code, 1)
        self.assertNotIn("secret-value-in-message", out + err)


if __name__ == "__main__":
    unittest.main()

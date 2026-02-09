from __future__ import annotations

import json
import pathlib
import shlex
import subprocess
import typing

import pulumi
import pulumi_command as c
import pulumi_gcp as g
import pulumi_hcloud as h
import google.auth
import googleapiclient.discovery
import requests


AnyDict = dict[str, typing.Any]


class DrivePermissionInputs(typing.TypedDict):
    fileId: str
    email: str
    role: str


class DrivePermissionOutputs(DrivePermissionInputs):
    permissionId: str


class WindmillVariableInputs(typing.TypedDict):
    workspace: str
    path: str
    value: str
    is_secret: bool
    description: str


class WindmillVariableOutputs(WindmillVariableInputs):
    pass


class Nix:
    """Evaluate Nix flake and system expressions and attributes."""

    root = pathlib.Path(__file__).parent

    class Eval:
        def __init__(
            self,
            args: list[str],
            *,
            impure: bool = False,
            format: typing.Literal["raw", "json"] = "raw",
        ):
            self._args = args
            self._impure = impure
            self._format = format

        def _clone(
            self,
            *,
            args: list[str] | None = None,
            impure: bool | None = None,
            format: typing.Literal["raw", "json"] | None = None,
        ) -> Nix.Eval:
            return Nix.Eval(
                args=args if args is not None else list(self._args),
                impure=self._impure if impure is None else impure,
                format=self._format if format is None else format,
            )

        def args(self, *extra: str) -> Nix.Eval:
            return self._clone(args=list(self._args) + list(extra))

        def impure(self, enabled: bool = True) -> Nix.Eval:
            return self._clone(impure=enabled)

        def json(self) -> Nix.Eval:
            return self._clone(format="json")

        def raw(self) -> Nix.Eval:
            return self._clone(format="raw")

        def apply(self, fn: str) -> Nix.Eval:
            # Transform the attribute in one evaluation via nix --apply.
            return self._clone(args=["--apply", fn, *self._args])

        def value(self) -> str:
            cmd_parts = ["nix", "eval", f"--{self._format}"]
            if self._impure:
                cmd_parts.append("--impure")
            cmd_parts.extend(self._args)
            cmd = shlex.join(cmd_parts)
            result = subprocess.run(cmd, shell=True, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError(f"Nix evaluation failed: {result.stderr.decode()}")
            return result.stdout.decode()

        def __str__(self) -> str:
            return self.value()

    @staticmethod
    def eval(*args: str) -> Nix.Eval:
        return Nix.Eval(list(args))

    @staticmethod
    def expr(_expr: str) -> Nix.Eval:
        return Nix.eval("--expr", _expr)

    @staticmethod
    def attr(_attr: str) -> Nix.Eval:
        return Nix.eval(f"{Nix.root}#{_attr}")

    @staticmethod
    def build(_attr: str) -> str:
        """Build a flake attribute and return its out path without linking."""
        cmd = shlex.join(["nix", "build", "--no-link", "--print-out-paths", f"{Nix.root}#{_attr}"])
        return subprocess.run(cmd, shell=True, capture_output=True).stdout.decode().strip()

    @staticmethod
    def bin(_attr: str) -> str:
        # Ensure the package is realized, then compute the executable path in one eval.
        package = Nix.build(_attr)
        exe = Nix.attr(_attr).apply("pkg: pkg.meta.mainProgram or pkg.pname").value().strip()
        return f"{package}/bin/{exe}"

    @staticmethod
    def system() -> str:
        return Nix.expr("builtins.currentSystem").impure().value()


class NixOS:
    """Deploy NixOS using nixos-anywhere."""

    def __init__(
        self,
        name: str,
        target: pulumi.Output[str],
        *flags: str,
        depends_on: list[pulumi.Resource] | None = None,
    ):
        nixos_anywhere = Nix.bin(f"canivete.inputs.nixos-anywhere.packages.{Nix.system()}.default")
        self.command = c.local.Command(
            f"nixos-{name}-install",
            create=target.apply(f"{nixos_anywhere} --flake {Nix.root}#{name} {' '.join(flags)}".__add__),  # type: ignore
            opts=pulumi.ResourceOptions(depends_on=depends_on or []),
        )


class DrivePermissionProvider(pulumi.dynamic.ResourceProvider):
    def create(self, props: AnyDict) -> pulumi.dynamic.CreateResult:
        inputs = typing.cast(DrivePermissionInputs, props)

        drive = self._get_drive_service()
        permission = (
            drive.permissions()
            .create(
                fileId=inputs["fileId"],
                body={"type": "user", "emailAddress": inputs["email"], "role": inputs["role"]},
            )
            .execute()
        )

        return pulumi.dynamic.CreateResult(id_=permission["id"], outs=inputs | dict(permissionId=permission["id"]))

    def update(self, _id: str, _olds: AnyDict, _news: AnyDict) -> pulumi.dynamic.UpdateResult:
        news = typing.cast(DrivePermissionInputs, _news)

        drive = self._get_drive_service()
        drive.permissions().update(fileId=news["fileId"], permissionId=_id, body={"role": news["role"]}).execute()

        return pulumi.dynamic.UpdateResult(outs=news | dict(permissionId=_id))

    def delete(self, _id: str, _props: AnyDict) -> None:
        props = typing.cast(DrivePermissionOutputs, _props)

        drive = self._get_drive_service()
        drive.permissions().delete(fileId=props["fileId"], permissionId=_id).execute()

    def _get_drive_service(self):
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
        return googleapiclient.discovery.build("drive", "v3", credentials=creds)


class DrivePermission(pulumi.dynamic.Resource):
    permission_id: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        file_id: pulumi.Input[str],
        email: pulumi.Input[str],
        role: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ):
        props = {"fileId": file_id, "email": email, "role": role}
        super().__init__(DrivePermissionProvider(), name, props, opts)
        self.permission_id = typing.cast(pulumi.Output[str], None)


class WindmillVariableProvider(pulumi.dynamic.ResourceProvider):
    def __init__(self):
        self.base_url = "https://app.windmill.dev"
        self.token = pulumi.Config("windmill").require_secret("token")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _url(self, workspace: str, subpath: str) -> str:
        return f"{self.base_url}/api/w/{workspace}/variables{subpath}"

    def _query(
        self,
        method: typing.Callable[..., requests.Response],
        workspace: str,
        subpath: str,
        body: AnyDict | None = None,
    ) -> requests.Response:
        url = self._url(workspace, subpath)
        response = method(url, headers=self._headers(), json=body)
        response.raise_for_status()
        return response

    def create(self, props: AnyDict) -> pulumi.dynamic.CreateResult:
        inputs = typing.cast(WindmillVariableInputs, props)
        workspace, path = inputs["workspace"], inputs["path"]

        check_response = self._query(requests.get, workspace, f"/get/{path}")
        if check_response.status_code == 404:
            body = {
                "path": path,
                "value": inputs["value"],
                "is_secret": inputs.get("is_secret", False),
                "description": inputs.get("description", ""),
            }
            self._query(requests.post, workspace, "/create", body)
        else:
            self._query(requests.post, workspace, f"/update/{path}", {"value": inputs["value"]})

        return pulumi.dynamic.CreateResult(id_=path, outs=inputs | {})

    def update(self, _id: str, _olds: AnyDict, _news: AnyDict) -> pulumi.dynamic.UpdateResult:
        news = typing.cast(WindmillVariableInputs, _news)
        self._query(requests.post, news["workspace"], f"/update/{_id}", {"value": news["value"]})

        return pulumi.dynamic.UpdateResult(outs=news | {})

    def delete(self, _id: str, _props: AnyDict) -> None:
        props = typing.cast(WindmillVariableOutputs, _props)
        self._query(requests.delete, props["workspace"], f"/delete/{_id}")


class WindmillVariable(pulumi.dynamic.Resource):
    def __init__(
        self,
        name: str,
        workspace: pulumi.Input[str],
        path: pulumi.Input[str],
        value: pulumi.Input[str],
        is_secret: pulumi.Input[bool] = False,
        description: pulumi.Input[str] = "",
        opts: pulumi.ResourceOptions | None = None,
    ):
        props = {
            "workspace": workspace,
            "path": path,
            "value": value,
            "is_secret": is_secret,
            "description": description,
        }
        super().__init__(WindmillVariableProvider(), name, props, opts)


def PrimaryIp(name: str, **kwargs) -> h.PrimaryIp:
    kwargs = (
        dict(
            name=name,
            assignee_type="server",
            location="ash",
            auto_delete=False,
        )
        | kwargs
    )
    return h.PrimaryIp(name, **kwargs)


def main():
    ipv4 = PrimaryIp("ipv4", type="ipv4")
    ipv6 = PrimaryIp("ipv6", type="ipv6")

    transform_attr = "builtins.mapAttrs (name: user: user.profiles.default.sshPubKey)"
    user_keys_eval = Nix.attr("canivete.meta.people.users").apply(transform_attr).json().value()
    user_keys = {
        user: h.SshKey(user, name=user, public_key=public_key)
        for user, public_key in json.loads(user_keys_eval).items()
    }

    # [tag:no-install-yet] Server variable not assigned, nixos install disabled
    # server = h.Server(
    h.Server(
        "servery",
        name="servery",
        location="ash",
        server_type="cpx11",
        image="debian-12",
        keep_disk=True,
        ssh_keys=[key.id for key in user_keys.values()],
        public_nets=[
            h.ServerPublicNetArgs(
                ipv4_enabled=True,
                ipv4=ipv4.id,  # type: ignore
                ipv6_enabled=True,
                ipv6=ipv6.id,  # type: ignore
            )
        ],
        opts=pulumi.ResourceOptions(ignore_changes=["ssh_keys"]),
    )

    # TODO deploy with nixos-anywhere
    # [ref:no-install-yet] NixOS install disabled until ready to deploy
    # NixOS("servery", server.ipv4_address.apply("root@".__add__))

    project_id = "tfc_womb"
    project = g.organizations.Project(project_id, name="Womb", project_id=project_id)
    for service in ["drive.googleapis.com", "sheets.googleapis.com"]:
        g.projects.Service(f"{project_id}/{service}", service=service, project=project.project_id)
    account_id = "windmill"
    service_account = g.serviceaccount.Account(
        f"projects/{project_id}/serviceAccounts/{account_id}@{project_id}.iam.gserviceaccount.com",
        account_id=account_id,
        project=project.project_id,
    )
    service_account_key = g.serviceaccount.Key(account_id, service_account_id=service_account.account_id)
    DrivePermission(
        f"sheet-{account_id}",
        file_id=pulumi.Config().require("gdrive_file_id"),
        email=service_account.email,
        role="writer",
    )
    WindmillVariable(
        "service_account_auth",
        workspace="terrace-f-club",
        path="u/tristanschrader/gdrive_creds",
        value=service_account_key.private_key,
        is_secret=True,
        description="Service Account credentials to read Google Drive files",
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import os
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

if typing.TYPE_CHECKING:
    from googleapiclient._apis.drive.v3 import DriveResource
    from googleapiclient._apis.drive.v3.schemas import File
    from googleapiclient._apis.sheets.v4 import SheetsResource
    from googleapiclient._apis.sheets.v4.schemas import (
        BatchUpdateSpreadsheetRequest,
        Request,
        Spreadsheet,
    )

AnyDict = dict[str, typing.Any]

# ============================================================================
# Dynamic Provider Input/Output Types (following official Pulumi pattern)
# ============================================================================


# DrivePermission types
class DrivePermissionInputs:
    """Public input class for DrivePermission resource (with pulumi.Input types)"""

    fileId: pulumi.Input[str]
    email: pulumi.Input[str]
    role: pulumi.Input[str]

    def __init__(self, fileId: pulumi.Input[str], email: pulumi.Input[str], role: pulumi.Input[str]):
        self.fileId = fileId
        self.email = email
        self.role = role


class _DrivePermissionProviderInputs(typing.TypedDict):
    """Provider input class for DrivePermission (unwrapped types for provider methods)"""

    fileId: str
    email: str
    role: str


class _DrivePermissionProviderOutputs(_DrivePermissionProviderInputs):
    """Provider output class for DrivePermission"""

    permissionId: str


# WindmillVariable types
class WindmillVariableInputs:
    """Public input class for WindmillVariable resource (with pulumi.Input types)"""

    workspace: pulumi.Input[str]
    path: pulumi.Input[str]
    value: pulumi.Input[str]
    is_secret: pulumi.Input[bool]
    description: pulumi.Input[str]

    def __init__(
        self,
        workspace: pulumi.Input[str],
        path: pulumi.Input[str],
        value: pulumi.Input[str],
        is_secret: pulumi.Input[bool] = False,
        description: pulumi.Input[str] = "",
    ):
        self.workspace = workspace
        self.path = path
        self.value = value
        self.is_secret = is_secret
        self.description = description


class _WindmillVariableProviderInputs(typing.TypedDict):
    """Provider input class for WindmillVariable (unwrapped types for provider methods)"""

    workspace: str
    path: str
    value: str
    is_secret: bool
    description: str


class _WindmillVariableProviderOutputs(_WindmillVariableProviderInputs):
    """Provider output class for WindmillVariable"""

    pass


# GoogleDriveFile types
class GoogleDriveFileInputs:
    """Public input class for GoogleDriveFile resource (with pulumi.Input types)"""

    name: pulumi.Input[str]
    mime_type: pulumi.Input[str]
    parent_folder_id: pulumi.Input[str] | None

    def __init__(
        self,
        name: pulumi.Input[str],
        mime_type: pulumi.Input[str] = "application/vnd.google-apps.spreadsheet",
        parent_folder_id: pulumi.Input[str] | None = None,
    ):
        self.name = name
        self.mime_type = mime_type
        self.parent_folder_id = parent_folder_id


class _GoogleDriveFileProviderInputs(typing.TypedDict):
    """Provider input class for GoogleDriveFile (unwrapped types for provider methods)"""

    name: str
    mime_type: str
    parent_folder_id: str | None


class _GoogleDriveFileProviderOutputs(_GoogleDriveFileProviderInputs):
    """Provider output class for GoogleDriveFile"""

    file_id: str
    web_view_link: str


# GoogleSheetSchema types
class GoogleSheetSchemaInputs:
    """Public input class for GoogleSheetSchema resource (with pulumi.Input types)"""

    spreadsheet_id: pulumi.Input[str]
    sheet_name: pulumi.Input[str]
    columns: pulumi.Input[list[str]]

    def __init__(
        self,
        spreadsheet_id: pulumi.Input[str],
        sheet_name: pulumi.Input[str],
        columns: pulumi.Input[list[str]],
    ):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.columns = columns


class _GoogleSheetSchemaProviderInputs(typing.TypedDict):
    """Provider input class for GoogleSheetSchema (unwrapped types for provider methods)"""

    spreadsheet_id: str
    sheet_name: str
    columns: list[str]


class _GoogleSheetSchemaProviderOutputs(_GoogleSheetSchemaProviderInputs):
    """Provider output class for GoogleSheetSchema"""

    sheet_id: int


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
        inputs = typing.cast(_DrivePermissionProviderInputs, props)

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
        news = typing.cast(_DrivePermissionProviderInputs, _news)

        drive = self._get_drive_service()
        drive.permissions().update(fileId=news["fileId"], permissionId=_id, body={"role": news["role"]}).execute()

        return pulumi.dynamic.UpdateResult(outs=news | dict(permissionId=_id))

    def delete(self, _id: str, _props: AnyDict) -> None:
        props = typing.cast(_DrivePermissionProviderOutputs, _props)

        drive = self._get_drive_service()
        drive.permissions().delete(fileId=props["fileId"], permissionId=_id).execute()

    def _get_drive_service(self):
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
        return googleapiclient.discovery.build("drive", "v3", credentials=creds)


class DrivePermission(pulumi.dynamic.Resource):
    """
    DrivePermission resource grants permissions to a Google Drive file.

    Outputs:
        permission_id: The ID of the created permission
    """

    permission_id: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        file_id: pulumi.Input[str],
        email: pulumi.Input[str],
        role: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ):
        # Create input props object
        input_props = DrivePermissionInputs(fileId=file_id, email=email, role=role)

        # Initialize outputs in props dict (will be populated by provider)
        super().__init__(
            DrivePermissionProvider(),
            name,
            {"permission_id": None, **vars(input_props)},
            opts,
        )


class WindmillVariableProvider(pulumi.dynamic.ResourceProvider):
    def __init__(self):
        self.base_url = "https://app.windmill.dev"
        self.token = os.getenv("WINDMILL_TOKEN", pulumi.Config("windmill").get_secret("token"))

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
        inputs = typing.cast(_WindmillVariableProviderInputs, props)
        workspace, path = inputs["workspace"], inputs["path"]

        exists_response = self._query(requests.get, workspace, f"/exists/{path}")
        exists = exists_response.text.strip().lower() == "true"

        if exists:
            self._query(requests.post, workspace, f"/update/{path}", {"value": inputs["value"]})
        else:
            body = {
                "path": path,
                "value": inputs["value"],
                "is_secret": inputs.get("is_secret", False),
                "description": inputs.get("description", ""),
            }
            self._query(requests.post, workspace, "/create", body)

        return pulumi.dynamic.CreateResult(id_=path, outs=inputs | {})

    def update(self, _id: str, _olds: AnyDict, _news: AnyDict) -> pulumi.dynamic.UpdateResult:
        news = typing.cast(_WindmillVariableProviderInputs, _news)
        self._query(requests.post, news["workspace"], f"/update/{_id}", {"value": news["value"]})

        return pulumi.dynamic.UpdateResult(outs=news | {})

    def delete(self, _id: str, _props: AnyDict) -> None:
        props = typing.cast(_WindmillVariableProviderOutputs, _props)
        self._query(requests.delete, props["workspace"], f"/delete/{_id}")


class WindmillVariable(pulumi.dynamic.Resource):
    """
    WindmillVariable resource creates/updates a variable in Windmill.

    No outputs beyond the inputs.
    """

    def __init__(
        self,
        name: str,
        path: pulumi.Input[str],
        value: pulumi.Input[str],
        is_secret: pulumi.Input[bool] = False,
        description: pulumi.Input[str] = "",
        workspace: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        # Default workspace if not provided
        default_workspace = (
            os.getenv("WINDMILL_WORKSPACE") or pulumi.Config("windmill").get("workspace") or "terrace-f-club"
        )

        # Create input props object
        input_props = WindmillVariableInputs(
            workspace=workspace or default_workspace,
            path=path,
            value=value,
            is_secret=is_secret,
            description=description,
        )

        # Initialize resource with props from input object
        super().__init__(WindmillVariableProvider(), name, {**vars(input_props)}, opts)


class GoogleDriveFileProvider(pulumi.dynamic.ResourceProvider):
    def create(self, props: AnyDict) -> pulumi.dynamic.CreateResult:
        inputs = typing.cast(_GoogleDriveFileProviderInputs, props)

        supports_all_drives = False
        file_metadata: File = {
            "name": inputs["name"],
            "mimeType": inputs["mime_type"],
        }
        if (folder := inputs.get("parent_folder_id")) is not None:
            file_metadata["parents"] = [folder]
            # Needed for shared drives
            supports_all_drives = True

        file = (
            self._get_drive_service()
            .files()
            .create(body=file_metadata, fields="id,webViewLink", supportsAllDrives=supports_all_drives)
            .execute()
        )

        return pulumi.dynamic.CreateResult(id_=file["id"], outs=self._outputs(inputs, file))

    def update(self, _id: str, _olds: AnyDict, _news: AnyDict) -> pulumi.dynamic.UpdateResult:
        news = typing.cast(_GoogleDriveFileProviderInputs, _news)

        update_metadata: File = {"name": news["name"]}
        file = (
            self._get_drive_service()
            .files()
            .update(
                fileId=_id,
                body=update_metadata,
                fields="id,webViewLink",
                supportsAllDrives=news.get("parent_folder_id") is not None,
            )
            .execute()
        )

        return pulumi.dynamic.UpdateResult(outs=self._outputs(news, file))

    def delete(self, _id: str, _props: AnyDict) -> None:
        props = typing.cast(_GoogleDriveFileProviderOutputs, _props)

        (
            self._get_drive_service()
            .files()
            .delete(fileId=_id, supportsAllDrives=props.get("parent_folder_id") is not None)
            .execute()
        )

    def _get_drive_service(self) -> DriveResource:
        import google.auth
        import googleapiclient.discovery

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
        return googleapiclient.discovery.build("drive", "v3", credentials=creds)

    @staticmethod
    def _outputs(inputs: _GoogleDriveFileProviderInputs, file: File) -> AnyDict:
        return inputs | {
            "file_id": file["id"],
            "web_view_link": file.get("webViewLink", ""),
        }


class GoogleDriveFile(pulumi.dynamic.Resource):
    """
    GoogleDriveFile resource creates a file in Google Drive.

    Outputs:
        file_id: The Google Drive file ID
        web_view_link: The web view link to the file
    """

    file_id: pulumi.Output[str]
    web_view_link: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        file_name: pulumi.Input[str],
        mime_type: pulumi.Input[str] = "application/vnd.google-apps.spreadsheet",
        parent_folder_id: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        # Create input props object
        input_props = GoogleDriveFileInputs(
            name=file_name,
            mime_type=mime_type,
            parent_folder_id=parent_folder_id,
        )

        # Initialize outputs in props dict (will be populated by provider)
        super().__init__(
            GoogleDriveFileProvider(),
            name,
            {"file_id": None, "web_view_link": None, **vars(input_props)},
            opts,
        )


class GoogleSheetSchemaProvider(pulumi.dynamic.ResourceProvider):
    def create(self, props: AnyDict) -> pulumi.dynamic.CreateResult:
        inputs = typing.cast(_GoogleSheetSchemaProviderInputs, props)
        sheets = self._get_sheets_service()
        spreadsheet = self._spreadsheet(sheets, inputs["spreadsheet_id"])
        sheet_id = self._sheet_id(spreadsheet, sheet_name := inputs["sheet_name"])
        if sheet_id is None:
            create_body: BatchUpdateSpreadsheetRequest = {
                "requests": [{"addSheet": {"properties": {"title": sheet_name}}}]
            }
            response = (
                sheets.spreadsheets()
                .batchUpdate(spreadsheetId=spreadsheet["spreadsheetId"], body=create_body)
                .execute()
            )
            sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]

        if (columns := inputs["columns"]) is not None:
            self._set_column_headers(sheets, spreadsheet, sheet_name, columns)
            format_request: Request = {
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                },
            }
            self._request(sheets, spreadsheet, format_request)

        resource_name = f"{inputs['spreadsheet_id']}/{sheet_id}"
        return pulumi.dynamic.CreateResult(id_=resource_name, outs=self._outputs(inputs, sheet_id))

    def update(self, _id: str, _olds: AnyDict, _news: AnyDict) -> pulumi.dynamic.UpdateResult:
        news = typing.cast(_GoogleSheetSchemaProviderInputs, _news)
        olds = typing.cast(_GoogleSheetSchemaProviderInputs, _olds)
        sheets = self._get_sheets_service()
        spreadsheet = self._spreadsheet(sheets, news["spreadsheet_id"])
        sheet_id = self._sheet_id(spreadsheet, sheet_name := news["sheet_name"])
        if sheet_id is None:
            raise ValueError(f"Sheet '{sheet_name}' not found")

        if (columns := news["columns"]) != olds["columns"]:
            self._set_column_headers(sheets, spreadsheet, sheet_name, columns)

        return pulumi.dynamic.UpdateResult(outs=self._outputs(news, sheet_id))

    def delete(self, _id: str, _props: AnyDict) -> None:
        props = typing.cast(_GoogleSheetSchemaProviderOutputs, _props)
        sheets = self._get_sheets_service()
        spreadsheet = self._spreadsheet(sheets, props["spreadsheet_id"])

        delete_request: Request = {"deleteSheet": {"sheetId": props["sheet_id"]}}
        self._request(sheets, spreadsheet, delete_request)

    def _get_sheets_service(self) -> SheetsResource:
        import google.auth
        import googleapiclient.discovery

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return googleapiclient.discovery.build("sheets", "v4", credentials=creds)

    def _spreadsheet(self, sheets: SheetsResource, id: str) -> Spreadsheet:
        return sheets.spreadsheets().get(spreadsheetId=id).execute()

    def _sheet_id(self, spreadsheet: Spreadsheet, sheet_name: str) -> int | None:
        for sheet in spreadsheet.get("sheets", []):
            if sheet["properties"]["title"] == sheet_name:
                return sheet["properties"]["sheetId"]
        return None

    @staticmethod
    def _set_column_headers(
        sheets: SheetsResource,
        spreadsheet: Spreadsheet,
        sheet_name: str,
        columns: list[str],
    ) -> None:
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet["spreadsheetId"],
            range=f"{sheet_name}!A1",
            valueInputOption="RAW",
            body={"values": [columns]},
        ).execute()

    @staticmethod
    def _request(
        sheets: SheetsResource,
        spreadsheet: Spreadsheet,
        request: Request,
    ) -> None:
        body: BatchUpdateSpreadsheetRequest = {"requests": [request]}
        sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet["spreadsheetId"], body=body).execute()

    @staticmethod
    def _outputs(inputs: _GoogleSheetSchemaProviderInputs, sheet_id: int) -> AnyDict:
        return inputs | {"sheet_id": sheet_id}


class GoogleSheetSchema(pulumi.dynamic.Resource):
    """
    GoogleSheetSchema resource initializes a sheet within a Google Spreadsheet with column headers.

    Outputs:
        sheet_id: The Google Sheets sheet ID (numeric)
    """

    sheet_id: pulumi.Output[int]

    def __init__(
        self,
        name: str,
        spreadsheet_id: pulumi.Input[str],
        sheet_name: pulumi.Input[str],
        columns: pulumi.Input[list[str]],
        opts: pulumi.ResourceOptions | None = None,
    ):
        # Create input props object
        input_props = GoogleSheetSchemaInputs(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            columns=columns,
        )

        # Initialize outputs in props dict (will be populated by provider)
        super().__init__(
            GoogleSheetSchemaProvider(),
            name,
            {"sheet_id": None, **vars(input_props)},
            opts,
        )


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

    project_id = pulumi.Config("gcp").require("project")
    project = g.organizations.get_project()

    services = {
        service: g.projects.Service(f"{project_id}/{service}", service=service, project=project.project_id)
        for service in ["drive.googleapis.com", "sheets.googleapis.com", "iam.googleapis.com"]
    }
    account_id = "windmill"
    service_account = g.serviceaccount.Account(
        f"projects/{project_id}/serviceAccounts/{account_id}@{project_id}.iam.gserviceaccount.com",
        account_id=account_id,
        project=project.project_id,
        opts=pulumi.ResourceOptions(depends_on=[services["iam.googleapis.com"]]),
    )
    service_account_key = g.serviceaccount.Key(account_id, service_account_id=service_account.account_id)
    WindmillVariable(
        "service_account_auth",
        path="f/Membership/gdrive_creds",
        value=service_account_key.private_key,
        is_secret=True,
        description="Service Account credentials to access Google Drive files",
    )

    sheet_custom_id = "membership-spreadsheet"
    membership_spreadsheet = GoogleDriveFile(
        sheet_custom_id,
        file_name="Membership Roster",
        mime_type="application/vnd.google-apps.spreadsheet",
        opts=pulumi.ResourceOptions(depends_on=[services["drive.googleapis.com"], services["sheets.googleapis.com"]]),
    )

    WindmillVariable(
        "membership_spreadsheet_id",
        path="f/Membership/membership_spreadsheet_id",
        value=membership_spreadsheet.file_id,
        description="Google Sheets ID for membership roster",
        opts=pulumi.ResourceOptions(parent=membership_spreadsheet),
    )

    DrivePermission(
        f"{sheet_custom_id}-{account_id}",
        file_id=membership_spreadsheet.file_id,
        email=service_account.email,
        role="writer",
        opts=pulumi.ResourceOptions(parent=membership_spreadsheet),
    )

    GoogleSheetSchema(
        f"{sheet_custom_id}-sophomores-2026-schema",
        spreadsheet_id=membership_spreadsheet.file_id,
        sheet_name="Sophomores 2026",
        # FIXME use the right columns
        columns=["name", "email", "membership", "installments", "status", "payment_date", "test1"],
        # columns=["name", "email", "membership", "installments", "status", "payment_date"],
        opts=pulumi.ResourceOptions(parent=membership_spreadsheet),
    )


if __name__ == "__main__":
    main()

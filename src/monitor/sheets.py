from datetime import datetime
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from common.aws import get_secret

TZ = ZoneInfo("Asia/Kolkata")


def current_file_name(now=None):
    now = now or datetime.now(TZ)
    return f"T20-20_{now.strftime('%b').upper()}_{now.strftime('%y')}"


def google_credentials(secret_arn):
    secret = get_secret(secret_arn)

    creds = Credentials(
        token=secret.get("token") or None,
        refresh_token=secret["refresh_token"],
        token_uri=secret.get(
            "token_uri",
            "https://oauth2.googleapis.com/token",
        ),
        client_id=secret["client_id"],
        client_secret=secret["client_secret"],
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )

    # Always obtain a fresh access token when Lambda starts.
    # The refresh token is the durable credential stored in Secrets Manager.
    creds.refresh(Request())

    return creds


def build_services(secret_arn):
    creds = google_credentials(secret_arn)

    return (
        build(
            "drive",
            "v3",
            credentials=creds,
            cache_discovery=False,
        ),
        build(
            "sheets",
            "v4",
            credentials=creds,
            cache_discovery=False,
        ),
    )


def find_spreadsheet(
    drive_service,
    expected_name,
    folder_id="",
):
    q = (
        "trashed = false "
        "and mimeType = 'application/vnd.google-apps.spreadsheet' "
        "and name = '{}'".format(
            expected_name.replace("'", "\\'")
        )
    )

    if folder_id:
        q += f" and '{folder_id}' in parents"

    files = (
        drive_service.files()
        .list(
            q=q,
            pageSize=20,
            fields=(
                "files("
                "id,name,mimeType,modifiedTime,parents,webViewLink"
                ")"
            ),
            orderBy="modifiedTime desc",
        )
        .execute()
        .get("files", [])
    )

    return files[0] if files else None


def get_sheet_rows(
    sheets_service,
    spreadsheet_id,
    read_range="A1:I250",
):
    return (
        sheets_service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=read_range,
        )
        .execute()
        .get("values", [])
    )
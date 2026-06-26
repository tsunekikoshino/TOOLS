import os
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

JST = ZoneInfo("Asia/Tokyo")
SCOPES = ["https://www.googleapis.com/auth/drive"]


def log(message: str):
    print(message, flush=True)
    os.makedirs("logs", exist_ok=True)
    with open("logs/run.log", "a", encoding="utf-8") as f:
        f.write(message + "\n")


def get_required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def drive_service():
    cred_path = get_required_env("GOOGLE_APPLICATION_CREDENTIALS")
    creds = service_account.Credentials.from_service_account_file(
        cred_path,
        scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def today_parts():
    now = datetime.now(JST)
    yyyy = now.strftime("%Y")
    yyyy_mm = now.strftime("%Y-%m")
    yyyy_mm_dd = now.strftime("%Y-%m-%d")
    yyyymmdd = now.strftime("%Y%m%d")
    return yyyy, yyyy_mm, yyyy_mm_dd, yyyymmdd


def find_child_folder(service, parent_id, folder_name):
    q = (
        f"'{parent_id}' in parents and "
        f"name = '{folder_name}' and "
        "mimeType = 'application/vnd.google-apps.folder' and "
        "trashed = false"
    )

    response = service.files().list(
        q=q,
        fields="files(id, name, mimeType, parents, shortcutDetails)",
        pageSize=20,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = response.get("files", [])
    log(f"find_child_folder parent={parent_id} name={folder_name} -> {files}")

    for f in files:
        if f.get("name") == folder_name and f.get("mimeType") == "application/vnd.google-apps.folder":
            return f["id"]

    return None


def require_child_folder(service, parent_id, folder_name):
    child_id = find_child_folder(service, parent_id, folder_name)
    if not child_id:
        raise RuntimeError(
            f"Folder not found: {folder_name} under parent {parent_id}"
        )
    return child_id


def find_file(service, parent_id: str, file_name: str):
    query = (
        f"'{parent_id}' in parents and "
        f"name='{file_name}' and trashed=false"
    )
    result = service.files().list(
        q=query,
        fields="files(id,name,mimeType)",
        pageSize=10
    ).execute()
    files = result.get("files", [])
    return files[0] if files else None


def download_file(service, file_id: str, local_path: str):
    request = service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def create_or_update_json(service, parent_id: str, filename: str, payload: dict):
    os.makedirs("/tmp/podcast_publish", exist_ok=True)
    local_path = f"/tmp/podcast_publish/{filename}"

    with open(local_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    media = MediaFileUpload(local_path, mimetype="application/json", resumable=False)
    existing = find_file(service, parent_id, filename)

    if existing:
        service.files().update(fileId=existing["id"], media_body=media).execute()
    else:
        metadata = {"name": filename, "parents": [parent_id]}
        service.files().create(body=metadata, media_body=media).execute()


def get_podbean_access_token():
    token_url = "https://api.podbean.com/v1/oauth/token"
    response = requests.post(
        token_url,
        data={"grant_type": "client_credentials"},
        auth=(
            get_required_env("PODBEAN_CLIENT_ID"),
            get_required_env("PODBEAN_CLIENT_SECRET")
        ),
        timeout=30
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Podbean access_token not found in token response")
    return token


def podbean_upload_and_publish(audio_path: str, payload: dict):
    access_token = get_podbean_access_token()

    filesize = os.path.getsize(audio_path)
    filename = os.path.basename(audio_path)

    authorize_url = "https://api.podbean.com/v1/files/uploadAuthorize"
    auth_response = requests.get(
        authorize_url,
        params={
            "access_token": access_token,
            "filename": filename,
            "filesize": filesize,
            "content_type": "audio/mpeg",
        },
        timeout=30
    )
    auth_response.raise_for_status()
    auth_data = auth_response.json()

    presigned_url = auth_data.get("presigned_url") or auth_data.get("url")
    file_key = auth_data.get("file_key") or auth_data.get("media_key")

    if not presigned_url:
        raise RuntimeError(f"Podbean uploadAuthorize response missing upload URL: {auth_data}")
    if not file_key:
        raise RuntimeError(f"Podbean uploadAuthorize response missing file_key/media_key: {auth_data}")

    with open(audio_path, "rb") as f:
        upload_response = requests.put(
            presigned_url,
            data=f,
            headers={"Content-Type": "audio/mpeg"},
            timeout=300
        )
    upload_response.raise_for_status()

    publish_url = "https://api.podbean.com/v1/episodes"
    publish_payload = {
        "access_token": access_token,
        "title": payload["episode_title"],
        "content": payload["episode_description"],
        "media_key": file_key,
        "status": "publish",
    }

    publish_response = requests.post(
        publish_url,
        data=publish_payload,
        timeout=60
    )
    publish_response.raise_for_status()
    return publish_response.json()


def main():
    log("=== podcast publish job started ===")

    root_folder_id = get_required_env("GOOGLE_DRIVE_FOLDER_ID")
    service = drive_service()

    yyyy, yyyy_mm, yyyy_mm_dd, yyyymmdd = today_parts()
    log(f"Target date: {yyyy_mm_dd}")

    year_folder_id = require_child_folder(service, root_folder_id, yyyy)
    month_folder_id = require_child_folder(service, year_folder_id, yyyy_mm)
    day_folder_id = require_child_folder(service, month_folder_id, yyyy_mm_dd)

    audio_name = f"podcast_audio_{yyyymmdd}.mp3"
    script_name = f"podcast_script_{yyyymmdd}.txt"
    payload_name = f"publish_payload_{yyyymmdd}.json"
    result_name = f"publish_result_{yyyymmdd}.json"
    error_name = f"publish_error_{yyyymmdd}.json"

    if find_file(service, day_folder_id, result_name):
        log("Already published. publish_result exists. Exit.")
        return

    audio_file = find_file(service, day_folder_id, audio_name)
    script_file = find_file(service, day_folder_id, script_name)
    payload_file = find_file(service, day_folder_id, payload_name)

    if not audio_file:
        raise RuntimeError(f"Missing required audio file: {audio_name}")
    if not script_file:
        raise RuntimeError(f"Missing required script file: {script_name}")
    if not payload_file:
        raise RuntimeError(f"Missing required payload file: {payload_name}")

    os.makedirs("/tmp/podcast_publish", exist_ok=True)
    local_audio = f"/tmp/podcast_publish/{audio_name}"
    local_payload = f"/tmp/podcast_publish/{payload_name}"

    log(f"Downloading: {audio_name}")
    download_file(service, audio_file["id"], local_audio)

    log(f"Downloading: {payload_name}")
    download_file(service, payload_file["id"], local_payload)

    with open(local_payload, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if payload.get("status") != "ready":
        raise RuntimeError(f"publish payload status is not ready: {payload.get('status')}")

    try:
        log("Publishing to Podbean...")
        result = podbean_upload_and_publish(local_audio, payload)

        result_payload = {
            "date_jst": payload.get("date_jst"),
            "status": "published",
            "published_at_jst": datetime.now(JST).isoformat(),
            "source_audio_filename": audio_name,
            "source_script_filename": script_name,
            "podbean_response": result,
        }

        create_or_update_json(service, day_folder_id, result_name, result_payload)
        log("Published successfully.")
        log("=== podcast publish job finished ===")

    except Exception as e:
        error_payload = {
            "date_jst": payload.get("date_jst"),
            "status": "error",
            "failed_at_jst": datetime.now(JST).isoformat(),
            "source_audio_filename": audio_name,
            "source_script_filename": script_name,
            "error_message": str(e),
        }
        create_or_update_json(service, day_folder_id, error_name, error_payload)
        log(f"Publish failed: {e}")
        raise


if __name__ == "__main__":
    main()

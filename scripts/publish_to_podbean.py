import io
import json
import mimetypes
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


JST = ZoneInfo("Asia/Tokyo")
SCOPES = ["https://www.googleapis.com/auth/drive"]


def log(message: str):
    print(message, flush=True)
    os.makedirs("logs", exist_ok=True)
    with open("logs/run.log", "a", encoding="utf-8") as f:
        f.write(message + "\n")


def save_json(path: str, data):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def drive_service():
    cred_path = get_required_env("GOOGLE_APPLICATION_CREDENTIALS")
    creds = service_account.Credentials.from_service_account_file(
        cred_path,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


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
        "trashed = false"
    )

    response = service.files().list(
        q=q,
        fields="files(id, name, mimeType, shortcutDetails)",
        pageSize=20,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = response.get("files", [])
    log(f"find_child_folder parent={parent_id} name={folder_name} -> {files}")

    for f in files:
        mime = f.get("mimeType")

        if mime == "application/vnd.google-apps.folder":
            return f["id"]

        if mime == "application/vnd.google-apps.shortcut":
            details = f.get("shortcutDetails") or {}
            target_id = details.get("targetId")
            target_mime = details.get("targetMimeType")
            if target_id and target_mime == "application/vnd.google-apps.folder":
                return target_id

    return None


def require_child_folder(service, parent_id, folder_name):
    child_id = find_child_folder(service, parent_id, folder_name)
    if not child_id:
        raise RuntimeError(f"Folder not found: {folder_name} under parent {parent_id}")
    return child_id


def find_file_in_folder(service, folder_id, file_name):
    q = (
        f"'{folder_id}' in parents and "
        f"name = '{file_name}' and "
        "trashed = false"
    )

    response = service.files().list(
        q=q,
        fields="files(id, name, mimeType, size)",
        pageSize=20,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = response.get("files", [])
    log(f"find_file_in_folder folder={folder_id} name={file_name} -> {files}")

    return files[0] if files else None


def require_file_in_folder(service, folder_id, file_name):
    f = find_file_in_folder(service, folder_id, file_name)
    if not f:
        raise RuntimeError(f"Required file not found: {file_name} in folder {folder_id}")
    return f


def download_drive_file(service, file_id, dest_path):
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                log(
                    f"download progress {os.path.basename(dest_path)}: "
                    f"{int(status.progress() * 100)}%"
                )
    log(f"downloaded: {dest_path}")


def get_podbean_access_token():
    url = "https://api.podbean.com/v1/oauth/token"
    data = {"grant_type": "client_credentials"}

    podcast_id = os.environ.get("PODBEAN_BLOG_ID", "").strip()
    if podcast_id:
        data["podcast_id"] = podcast_id

    response = requests.post(
        url,
        data=data,
        auth=(
            get_required_env("PODBEAN_CLIENT_ID"),
            get_required_env("PODBEAN_CLIENT_SECRET"),
        ),
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()

    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"Podbean access token not found: {payload}")

    log("Podbean access token acquired")
    return access_token


def authorize_file_upload(access_token, file_path):
    url = "https://api.podbean.com/v1/files/uploadAuthorize"
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    content_type = mimetypes.guess_type(file_path)[0] or "audio/mpeg"

    response = requests.post(
        url,
        data={
            "access_token": access_token,
            "file_name": file_name,
            "file_size": str(file_size),
            "content_type": content_type,
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()

    presigned_url = payload.get("presigned_url")
    file_key = payload.get("file_key")
    if not presigned_url or not file_key:
        raise RuntimeError(f"Invalid uploadAuthorize response: {payload}")

    log(f"upload authorized: file_name={file_name}, file_key={file_key}")
    return presigned_url, file_key, content_type


def upload_binary_to_presigned_url(presigned_url, file_path, content_type):
    with open(file_path, "rb") as f:
        response = requests.put(
            presigned_url,
            data=f,
            headers={"Content-Type": content_type},
            timeout=600,
        )

    if response.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"Upload to presigned_url failed: "
            f"status={response.status_code}, body={response.text[:500]}"
        )

    log(f"uploaded audio to storage: {os.path.basename(file_path)}")


def publish_episode(access_token, payload, media_key, fallback_content):
    title = str(payload.get("title", "")).strip()
    if not title:
        raise RuntimeError("publish_payload JSON に title がありません")

    content = str(
        payload.get("content")
        or payload.get("description")
        or fallback_content
        or ""
    )

    data = {
        "access_token": access_token,
        "title": title,
        "content": content,
        "media_key": media_key,
        "status": str(payload.get("podbean_status", "publish")),
        "type": str(payload.get("episode_type", "public")),
    }

    if payload.get("logo_key"):
        data["logo_key"] = str(payload["logo_key"])

    response = requests.post(
        "https://api.podbean.com/v1/episodes",
        data=data,
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()

    log(f"episode publish response: {json.dumps(result, ensure_ascii=False)[:1000]}")
    return result


def main():
    log("=== podcast publish job started ===")

    root_folder_id = get_required_env("GOOGLE_DRIVE_FOLDER_ID")
    service = drive_service()

    yyyy, yyyy_mm, yyyy_mm_dd, yyyymmdd = today_parts()
    log(f"Target date: {yyyy_mm_dd}")

    month_folder_id = require_child_folder(service, root_folder_id, yyyy_mm)
    day_folder_id = require_child_folder(service, month_folder_id, yyyy_mm_dd)

    audio_name = f"podcast_audio_{yyyymmdd}.mp3"
    script_name = f"podcast_script_{yyyymmdd}.txt"
    payload_name = f"publish_payload_{yyyymmdd}.json"

    audio_file = require_file_in_folder(service, day_folder_id, audio_name)
    script_file = require_file_in_folder(service, day_folder_id, script_name)
    payload_file = require_file_in_folder(service, day_folder_id, payload_name)

    log(f"Found audio file: {audio_file['name']} ({audio_file.get('id')})")
    log(f"Found script file: {script_file['name']} ({script_file.get('id')})")
    log(f"Found payload file: {payload_file['name']} ({payload_file.get('id')})")

    os.makedirs("work", exist_ok=True)
    audio_path = os.path.join("work", audio_name)
    script_path = os.path.join("work", script_name)
    payload_path = os.path.join("work", payload_name)

    download_drive_file(service, audio_file["id"], audio_path)
    download_drive_file(service, script_file["id"], script_path)
    download_drive_file(service, payload_file["id"], payload_path)

    with open(script_path, "r", encoding="utf-8") as f:
        script_text = f.read()

    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    handoff_status = str(payload.get("status", "")).strip().lower()
    if handoff_status != "ready":
        raise RuntimeError(
            f"publish_payload status must be 'ready', got: {payload.get('status')}"
        )

    log(f"Payload title: {payload.get('title', '')}")
    log(f"Script length: {len(script_text)} chars")

    access_token = get_podbean_access_token()
    presigned_url, media_key, content_type = authorize_file_upload(access_token, audio_path)
    upload_binary_to_presigned_url(presigned_url, audio_path, content_type)
    result = publish_episode(access_token, payload, media_key, script_text)

    save_json("logs/publish_result.json", result)

    episode_id = result.get("id") or result.get("episode", {}).get("id")
    permalink = result.get("permalink_url") or result.get("permalink")
    media_url = result.get("media_url")

    log(f"Episode ID: {episode_id}")
    if permalink:
        log(f"Permalink: {permalink}")
    if media_url:
        log(f"Media URL: {media_url}")

    log("=== podcast publish job finished ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        save_json("logs/publish_error.json", {"error": str(e)})
        log(f"ERROR: {e}")
        raise

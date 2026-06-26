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

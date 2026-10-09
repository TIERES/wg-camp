"""Uploads a file to archive.org via its S3-like API
(https://archive.org/developers/ias3.html) - a single PUT with an
Authorization header. No extra dependency needed (this app has none beyond
Flask/gunicorn): http.client streams a file object in chunks on its own, so
this never loads the file into memory.
"""
import http.client
import json
import os
import urllib.parse
import urllib.request


class ArchiveOrgError(Exception):
    pass


def download_url(identifier, filename):
    return f"https://archive.org/download/{urllib.parse.quote(identifier)}/{urllib.parse.quote(filename)}"


def upload_file(identifier, file_path, access_key, secret_key, remote_name=None,
                content_type="application/octet-stream", item_metadata=None):
    """PUTs file_path into item `identifier` as `remote_name` (defaults to
    the local basename). With `item_metadata` ({"collection":..,
    "title":.., ...}) the item is created on first upload; without it the
    item must already exist, and its metadata is left untouched. Returns
    the file's direct download URL. Raises ArchiveOrgError on any non-2xx
    response or connection failure."""
    if not access_key or not secret_key:
        raise ArchiveOrgError("Chaves do archive.org não configuradas.")

    filename = remote_name or os.path.basename(file_path)
    size = os.path.getsize(file_path)

    headers = {
        "authorization": f"LOW {access_key}:{secret_key}",
        "x-archive-size-hint": str(size),
        "Content-Length": str(size),
        "Content-Type": content_type,
    }
    if item_metadata is not None:
        headers["x-archive-auto-make-bucket"] = "1"
        headers["x-archive-meta-mediatype"] = "data"
        for key, value in item_metadata.items():
            headers[f"x-archive-meta-{key}"] = value
    path = f"/{urllib.parse.quote(identifier)}/{urllib.parse.quote(filename)}"

    conn = http.client.HTTPSConnection("s3.us.archive.org", timeout=300)
    try:
        with open(file_path, "rb") as f:
            conn.request("PUT", path, body=f, headers=headers)
            response = conn.getresponse()
            body = response.read()
    except OSError as error:
        raise ArchiveOrgError(f"Falha de rede ao enviar para o archive.org: {error}") from error
    finally:
        conn.close()

    if response.status not in (200, 201):
        raise ArchiveOrgError(f"archive.org retornou {response.status}: {body[:300].decode('utf-8', 'replace')}")

    return download_url(identifier, filename)


def upload_zip(identifier, file_path, access_key, secret_key, collection, title, description=""):
    """Uploads file_path to archive.org as a new item `identifier`
    (created automatically on first upload). Returns the item's public
    URL."""
    upload_file(
        identifier, file_path, access_key, secret_key,
        content_type="application/zip",
        item_metadata={"collection": collection, "title": title, "description": description},
    )
    return f"https://archive.org/details/{identifier}"


def delete_file(identifier, filename, access_key, secret_key):
    """Deletes one file (and the derivatives archive.org generated from it)
    from item `identifier`. Raises ArchiveOrgError on any non-2xx response
    or connection failure."""
    if not access_key or not secret_key:
        raise ArchiveOrgError("Chaves do archive.org não configuradas.")
    headers = {
        "authorization": f"LOW {access_key}:{secret_key}",
        "x-archive-cascade-delete": "1",
    }
    path = f"/{urllib.parse.quote(identifier)}/{urllib.parse.quote(filename)}"
    conn = http.client.HTTPSConnection("s3.us.archive.org", timeout=120)
    try:
        conn.request("DELETE", path, headers=headers)
        response = conn.getresponse()
        body = response.read()
    except OSError as error:
        raise ArchiveOrgError(f"Falha de rede ao excluir do archive.org: {error}") from error
    finally:
        conn.close()
    if response.status not in (200, 204):
        raise ArchiveOrgError(f"archive.org recusou a exclusão ({response.status}): {body[:300].decode('utf-8', 'replace')}")


def list_item_files(identifier):
    """Returns {name: {"md5", "crc32", "size", "mtime"}} for the item's original
    files ({} if the item doesn't exist yet). Raises ArchiveOrgError when
    archive.org can't be reached."""
    url = f"https://archive.org/metadata/{urllib.parse.quote(identifier)}/files"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as error:
        raise ArchiveOrgError(f"Falha ao consultar os arquivos do archive.org: {error}") from error
    return {
        entry["name"]: {"md5": entry.get("md5"), "crc32": entry.get("crc32"), "size": int(entry.get("size") or 0),
                        "mtime": entry.get("mtime")}
        for entry in data.get("result", [])
        if entry.get("source") == "original"
    }

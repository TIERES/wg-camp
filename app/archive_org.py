"""Uploads a file to archive.org via its S3-like API
(https://archive.org/developers/ias3.html) - a single PUT with an
Authorization header. No extra dependency needed (this app has none beyond
Flask/gunicorn): http.client streams a file object in chunks on its own, so
this never loads the backup zip into memory.
"""
import http.client
import os
import urllib.parse


class ArchiveOrgError(Exception):
    pass


def upload_zip(identifier, file_path, access_key, secret_key, collection, title, description=""):
    """Uploads file_path to archive.org as a new item `identifier`
    (created automatically on first upload). Returns the item's public
    URL. Raises ArchiveOrgError on any non-2xx response or connection
    failure."""
    if not access_key or not secret_key:
        raise ArchiveOrgError("Chaves do archive.org não configuradas.")

    filename = os.path.basename(file_path)
    size = os.path.getsize(file_path)

    headers = {
        "authorization": f"LOW {access_key}:{secret_key}",
        "x-archive-auto-make-bucket": "1",
        "x-archive-meta-mediatype": "data",
        "x-archive-meta-collection": collection,
        "x-archive-meta-title": title,
        "x-archive-meta-description": description,
        "Content-Length": str(size),
        "Content-Type": "application/zip",
    }
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

    return f"https://archive.org/details/{identifier}"

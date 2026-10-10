import hashlib
import os
import re
import unicodedata
import uuid
import zlib
from pathlib import Path
from urllib.parse import quote

from flask import current_app
from werkzeug.datastructures import FileStorage

# What Windows can't have in a file name (and control characters).
_WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_original_filename(name):
    """The uploaded file's own name, as players see it on archive.org and in
    their download folder: spaces and accents kept (werkzeug's
    secure_filename turned "WE 2002.bin" into "WE_2002.bin"). Only the path
    and what Windows can't have in a file name go away."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _WINDOWS_INVALID.sub("", unicodedata.normalize("NFC", name))
    name = " ".join(name.split()).strip(" .")
    if len(name) > 200:
        stem, dot, extension = name.rpartition(".")
        name = (stem[:199 - len(extension)].rstrip(" .") + dot + extension) if dot else name[:200]
    return name


def attachment_header(name):
    """Content-Disposition for a name with spaces/accents (RFC 6266): an
    ASCII fallback for old clients plus the UTF-8 name."""
    fallback = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").replace('"', "")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def human_size(size):
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def store_upload(upload: FileStorage):
    original = clean_original_filename(upload.filename)
    if not original or "." not in original:
        raise ValueError("Informe um arquivo com extensão permitida.")
    extension = original.rsplit(".", 1)[1].lower()
    if extension not in current_app.config["ALLOWED_EXTENSIONS"]:
        raise ValueError("Extensão de arquivo não permitida.")

    stored_name = f"{uuid.uuid4().hex}.{extension}"
    temp_path = Path(current_app.config["UPLOAD_TMP_DIR"]) / f"{stored_name}.part"
    final_path = Path(current_app.config["DOWNLOADS_DIR"]) / stored_name
    digest = hashlib.sha256()
    md5 = hashlib.md5()
    crc32 = 0
    size = 0
    try:
        with temp_path.open("xb") as target:
            while chunk := upload.stream.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
                md5.update(chunk)
                crc32 = zlib.crc32(chunk, crc32)
                size += len(chunk)
        os.replace(temp_path, final_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return {"stored_name": stored_name, "original_filename": original, "file_size": size, "sha256": digest.hexdigest(), "md5": md5.hexdigest(), "crc32": crc32}


def delete_stored_file(stored_name):
    if not re.fullmatch(r"[a-f0-9]{32}\.[a-z0-9]+", stored_name):
        raise ValueError("Nome interno de arquivo inválido.")
    (Path(current_app.config["DOWNLOADS_DIR"]) / stored_name).unlink(missing_ok=True)

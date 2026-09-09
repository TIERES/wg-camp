import hashlib
import os
import re
import uuid
from pathlib import Path

from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


def human_size(size):
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def store_upload(upload: FileStorage):
    original = secure_filename(upload.filename or "")
    if not original or "." not in original:
        raise ValueError("Informe um arquivo com extensão permitida.")
    extension = original.rsplit(".", 1)[1].lower()
    if extension not in current_app.config["ALLOWED_EXTENSIONS"]:
        raise ValueError("Extensão de arquivo não permitida.")

    stored_name = f"{uuid.uuid4().hex}.{extension}"
    temp_path = Path(current_app.config["UPLOAD_TMP_DIR"]) / f"{stored_name}.part"
    final_path = Path(current_app.config["DOWNLOADS_DIR"]) / stored_name
    digest = hashlib.sha256()
    size = 0
    try:
        with temp_path.open("xb") as target:
            while chunk := upload.stream.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        os.replace(temp_path, final_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return {"stored_name": stored_name, "original_filename": original, "file_size": size, "sha256": digest.hexdigest()}


def delete_stored_file(stored_name):
    if not re.fullmatch(r"[a-f0-9]{32}\.[a-z0-9]+", stored_name):
        raise ValueError("Nome interno de arquivo inválido.")
    (Path(current_app.config["DOWNLOADS_DIR"]) / stored_name).unlink(missing_ok=True)

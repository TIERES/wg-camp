import zlib
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, request, send_from_directory

bp = Blueprint("updates", __name__, url_prefix="/updates")

# Matches the kailleraclient DLL's own two build flavors (see _WIN64 in
# common/n02_update.cpp) - a DLL always knows its own architecture at
# compile time, so it only ever asks for the one that matches itself.
VALID_ARCHES = ("x64", "x86")


def _updates_dir():
    path = Path(current_app.config["UPDATES_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dll_path(arch):
    return _updates_dir() / f"kailleraclient-{arch}.dll"


@bp.get("/latest")
def latest():
    """Version-check for the kailleraclient DLL's self-updater.

    See common/n02_update.cpp (kaillera-client repo) for the client side.
    ?arch=x64|x86 is required. Response is one tab-separated line:

        version\tsize_bytes\tcrc32_hex

    or 404 if no build for that arch has been published yet (see
    deploy/publish_update.sh). Plain text, no JSON/TLS - the client speaks
    raw HTTP with no JSON library, same reasoning as replays/list.txt.
    size_bytes/crc32 let the client verify its download (over plain HTTP,
    to a file it's about to swap in for itself) before committing to it.
    """
    arch = request.args.get("arch", "")
    if arch not in VALID_ARCHES:
        abort(400, "Parâmetro 'arch' inválido - use x64 ou x86.")

    version_file = _updates_dir() / "version.txt"
    dll_file = _dll_path(arch)
    if not version_file.exists() or not dll_file.exists():
        abort(404, "Nenhuma versão publicada ainda.")

    version = version_file.read_text(encoding="utf-8").strip()
    data = dll_file.read_bytes()
    crc = zlib.crc32(data) & 0xffffffff

    return Response(f"{version}\t{len(data)}\t{crc:08x}\n", mimetype="text/plain")


@bp.get("/download/<arch>")
def download(arch):
    """Serves the published kailleraclient.dll for `arch` as a raw octet
    stream - the client downloads this to a temp file, checks it against
    the size/crc32 from /latest, and only then swaps it in for itself."""
    if arch not in VALID_ARCHES:
        abort(400, "Arquitetura inválida.")
    if not _dll_path(arch).exists():
        abort(404)
    # download_name is always "kailleraclient.dll" (not "-x64"/"-x86"), matching
    # the exact filename the emulator expects it dropped in as - so a browser
    # download can be copied straight into place with no renaming needed.
    return send_from_directory(
        _updates_dir(), _dll_path(arch).name, mimetype="application/octet-stream",
        as_attachment=True, download_name="kailleraclient.dll",
    )

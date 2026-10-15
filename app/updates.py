import re
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


# Pacote completo do RetroArch TIERES (o mesmo conteúdo da versão light:
# retroarch.exe, kailleraclient.dll, núcleo): o DLL x64, rodando dentro do
# retroarch.exe TIERES, baixa só os arquivos que mudaram, troca todos e
# reinicia o RetroArch (common/n02_update.cpp, repo kaillera-client).
#
# PACOTE_DIR/x64.txt é o manifesto, linhas separadas por tab:
#
#     versao   v.TIERES.0.25                      (versão do DLL do pacote)
#     pacote   RetroArch-1.16.0.FFW.TIERES.0.6    (nome do zip light)
#     arquivo  <sha256>  <bytes>  retroarch.exe   (um por arquivo)
#
# e PACOTE_DIR/arquivos/<sha256> são os arquivos, pelo conteúdo: publicar uma
# versão só acrescenta os que ainda não existem e troca o manifesto por
# último, então quem baixa no meio da publicação nunca pega metade de cada.
# Quem publica é o deploy/publish_pacote.sh, a partir da task "Publish release
# to updater" do kaillera-client (sem zip: o servidor só copia arquivos).
#
# Em produção o Caddy serve /updates/pacote/* direto da pasta (são uns 25 MB
# por jogador, e o gunicorn só tem 2 threads); estas rotas são o mesmo
# caminho para o desenvolvimento, os testes e o caso do Caddy sem a regra.
PACOTE_ARCHES = ("x64",)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _pacote_dir():
    return Path(current_app.config["PACOTE_DIR"])


@bp.get("/pacote/<arch>.txt")
def pacote_manifesto(arch):
    if arch not in PACOTE_ARCHES or not (_pacote_dir() / f"{arch}.txt").is_file():
        abort(404)
    response = send_from_directory(_pacote_dir(), f"{arch}.txt", mimetype="text/plain", max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.get("/pacote/arquivos/<sha256>")
def pacote_arquivo(sha256):
    if not _SHA256.fullmatch(sha256):
        abort(404)
    return send_from_directory(_pacote_dir() / "arquivos", sha256, mimetype="application/octet-stream")

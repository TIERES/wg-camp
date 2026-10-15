#!/usr/bin/env bash
# Publica o pacote do RetroArch TIERES (o conteúdo da versão light) para o
# atualizador do DLL (app/updates.py, rotas /updates/pacote/*). Roda no
# servidor como root, chamado pela task "Publish release to updater" do
# kaillera-client (deploy/publish_release.sh lá):
#
#   sudo bash deploy/publish_pacote.sh <x64.txt> <pasta com os arquivos novos>
#
# A pasta traz só os arquivos que o servidor ainda não tinha, cada um com o
# próprio sha256 como nome (pode estar vazia). Nada é descompactado nem
# gerado aqui: o script confere o sha256 de cada arquivo novo, copia, confere
# se tudo o que o manifesto cita está na pasta com o tamanho certo e só então
# troca o manifesto. O anterior fica em x64.anterior.txt; os arquivos que nem
# o manifesto novo nem o anterior citam são apagados.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "Uso: $0 <x64.txt> <pasta com os arquivos novos>" >&2
    exit 1
fi

MANIFESTO="$1"
NOVOS="$2"
DIR="${ARENA17_PACOTE_DIR:-/var/www/arena17/pacote}"
ARQUIVOS="$DIR/arquivos"

[ -f "$MANIFESTO" ] || { echo "Manifesto não encontrado: $MANIFESTO" >&2; exit 1; }
[ -d "$NOVOS" ] || { echo "Pasta não encontrada: $NOVOS" >&2; exit 1; }
grep -q $'^versao\t' "$MANIFESTO" || { echo "Manifesto sem a linha 'versao'." >&2; exit 1; }
grep -q $'^arquivo\t' "$MANIFESTO" || { echo "Manifesto sem nenhum 'arquivo'." >&2; exit 1; }

mkdir -p "$ARQUIVOS"

# 1. Arquivos novos: o nome tem que ser o sha256 do conteúdo.
for f in "$NOVOS"/*; do
    [ -e "$f" ] || continue
    nome=$(basename "$f")
    [[ "$nome" =~ ^[0-9a-f]{64}$ ]] || { echo "Nome inválido: $nome" >&2; exit 1; }
    sha=$(sha256sum "$f" | cut -d' ' -f1)
    [ "$sha" = "$nome" ] || { echo "sha256 não confere (envio corrompido?): $nome" >&2; exit 1; }
    cp "$f" "$ARQUIVOS/$nome.tmp"
    mv -f "$ARQUIVOS/$nome.tmp" "$ARQUIVOS/$nome"
done

# 2. Tudo o que o manifesto cita tem que estar aqui, com o tamanho certo.
while IFS=$'\t' read -r tipo sha tamanho caminho; do
    [ "$tipo" = arquivo ] || continue
    [ -f "$ARQUIVOS/$sha" ] || { echo "Falta no servidor: $caminho ($sha)" >&2; exit 1; }
    [ "$(stat -c %s "$ARQUIVOS/$sha")" = "$tamanho" ] || { echo "Tamanho não confere: $caminho" >&2; exit 1; }
done < "$MANIFESTO"

# 3. Troca o manifesto (de uma vez: quem pede no meio vê o velho ou o novo).
if [ -f "$DIR/x64.txt" ]; then
    cp -p "$DIR/x64.txt" "$DIR/x64.anterior.txt"
fi
cp "$MANIFESTO" "$DIR/x64.txt.tmp"
mv -f "$DIR/x64.txt.tmp" "$DIR/x64.txt"

# 4. Limpeza: fica o que o manifesto atual ou o anterior citam.
manifestos=("$DIR/x64.txt")
if [ -f "$DIR/x64.anterior.txt" ]; then
    manifestos+=("$DIR/x64.anterior.txt")
fi
citados=$(awk -F'\t' '$1 == "arquivo" { print $2 }' "${manifestos[@]}" | sort -u)
for f in "$ARQUIVOS"/*; do
    [ -e "$f" ] || continue
    nome=$(basename "$f")
    if ! grep -qxF "$nome" <<<"$citados"; then
        rm -f "$f"
        echo "Removido (nenhum manifesto cita): $nome"
    fi
done

# Legível pelo Caddy (serve /updates/pacote/* direto) e pelo app.
if id arena17 >/dev/null 2>&1; then
    chown -R arena17:arena17 "$DIR"
fi
chmod 755 "$DIR" "$ARQUIVOS"
find "$DIR" -type f -exec chmod 644 {} +

echo "Pacote publicado:"
cat "$DIR/x64.txt"

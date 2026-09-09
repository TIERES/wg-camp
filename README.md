# Arena17 Downloads

Aplicação leve para publicar campeonatos de Winning Eleven 2002 e os seus arquivos.

O Flask guarda metadados e administra uploads. O Nginx entrega os arquivos de
`/var/www/arena17/downloads` diretamente, sem transferi-los pelo processo Python.

Localmente, na ausência de variáveis de ambiente, os arquivos são mantidos em
`storage/`. Em produção, o serviço systemd define os diretórios em `/var/www/arena17/`.

## Desenvolvimento local

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ARENA17_SECRET_KEY = "troque-por-um-segredo-longo"
flask --app wsgi init-db
flask --app wsgi create-admin admin
flask --app wsgi run --debug
```

Em produção, configure as variáveis no arquivo de ambiente do systemd. Não use
o servidor de desenvolvimento Flask.

## Variáveis de ambiente

- `ARENA17_SECRET_KEY`: obrigatório em produção.
- `ARENA17_INSTANCE_PATH`: diretório do SQLite e da chave, padrão `instance/`.
- `ARENA17_DOWNLOADS_DIR`: diretório final dos downloads.
- `ARENA17_UPLOAD_TMP_DIR`: diretório temporário, fora do diretório público.
- `ARENA17_MAX_UPLOAD_BYTES`: máximo por upload, padrão 8 GiB.

Os diretórios de upload precisam pertencer ao usuário da aplicação. O diretório
do banco não deve ser exposto pelo Nginx.

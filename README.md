# Arena17 Downloads

Aplicação leve para publicar campeonatos de Winning Eleven 2002, suas ligas e arquivos.

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

Para criar administradores posteriormente, execute `python create_admin.py` no
diretório da aplicação. O script solicita o usuário e a senha duas vezes.

Em produção, configure as variáveis no arquivo de ambiente do systemd. Não use
o servidor de desenvolvimento Flask.

## Variáveis de ambiente

- `ARENA17_SECRET_KEY`: obrigatório em produção.
- `ARENA17_INSTANCE_PATH`: diretório do SQLite e da chave, padrão `instance/`.
- `ARENA17_DOWNLOADS_DIR`: diretório final dos downloads.
- `ARENA17_UPLOAD_TMP_DIR`: diretório temporário, fora do diretório público.
- `ARENA17_MAX_UPLOAD_BYTES`: máximo por upload, padrão 8 GiB.
- `ARENA17_SERVE_DOWNLOADS_LOCALLY`: use `true` somente em testes locais sem Nginx.

Os diretórios de upload precisam pertencer ao usuário da aplicação. O diretório
do banco não deve ser exposto pelo Nginx.

Em produção, o botão de download chama o Flask somente para autorizar o arquivo.
Ele responde com `X-Accel-Redirect`, e o Nginx entrega o arquivo internamente;
assim, links de arquivos não ficam expostos como diretórios públicos.

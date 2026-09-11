@echo off
setlocal

rem Execute this script from any directory.
cd /d "%~dp0"

rem Prefer a project virtual environment; otherwise use the Python launcher.
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul
    if errorlevel 1 (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python nao foi encontrado. Instale Python 3 e execute este arquivo novamente.
            pause
            exit /b 1
        )
        set "PYTHON=python"
    ) else (
        set "PYTHON=py -3"
    )
)

rem Install project dependencies only when Flask is missing.
call %PYTHON% -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo Instalando dependencias do projeto...
    call %PYTHON% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Nao foi possivel instalar as dependencias.
        pause
        exit /b 1
    )
)

rem Safe defaults for local development. Existing environment values take priority.
if not defined ARENA17_SECRET_KEY set "ARENA17_SECRET_KEY=arena17-local-development-only"
if not defined ARENA17_SERVE_DOWNLOADS_LOCALLY set "ARENA17_SERVE_DOWNLOADS_LOCALLY=true"

echo Inicializando o banco de dados...
call %PYTHON% -m flask --app wsgi init-db
if errorlevel 1 (
    echo Nao foi possivel inicializar o banco de dados.
    pause
    exit /b 1
)

echo.
echo Servidor disponivel em http://localhost:5000
echo Pressione Ctrl+C para encerrar.
rem 0.0.0.0 avoids a Python 3.14 DNS-decoding issue seen with 127.0.0.1.
call %PYTHON% -m flask --app wsgi run --debug --host 0.0.0.0 --port 5000

endlocal

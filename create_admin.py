"""Create an administrator interactively.

Run from the project directory with the same Python environment as the app:
    python create_admin.py
"""

import getpass
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
vendor = ROOT / "vendor"
if vendor.is_dir():
    sys.path.insert(0, str(vendor))

os.environ.setdefault("ARENA17_INSTANCE_PATH", str(ROOT / "instance"))

from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db, now


def main():
    username = input("Usuário: ").strip()
    if not username:
        raise SystemExit("O usuário é obrigatório.")
    password = getpass.getpass("Senha: ")
    confirmation = getpass.getpass("Confirme a senha: ")
    if not password:
        raise SystemExit("A senha é obrigatória.")
    if password != confirmation:
        raise SystemExit("As senhas não coincidem.")

    app = create_app()
    with app.app_context():
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username,password_hash,created_at,updated_at) VALUES (?,?,?,?)",
                (username, generate_password_hash(password), now(), now()),
            )
            db.commit()
        except Exception as error:
            db.rollback()
            if "UNIQUE" in str(error).upper():
                raise SystemExit("Esse usuário já existe.") from error
            raise
    print(f"Usuário {username!r} criado com sucesso.")


if __name__ == "__main__":
    main()

import os
from pathlib import Path

from flask import Flask

from . import db


def create_app(test_config=None):
    root = Path(__file__).resolve().parent.parent
    instance_path = Path(os.environ.get("ARENA17_INSTANCE_PATH", root / "instance"))
    app = Flask(__name__, instance_path=str(instance_path), instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("ARENA17_SECRET_KEY", "development-only-change-me"),
        DATABASE=str(instance_path / "arena17.db"),
        DOWNLOADS_DIR=os.environ.get("ARENA17_DOWNLOADS_DIR", str(root / "storage" / "downloads")),
        UPLOAD_TMP_DIR=os.environ.get("ARENA17_UPLOAD_TMP_DIR", str(root / "storage" / "uploads-tmp")),
        MAX_CONTENT_LENGTH=int(os.environ.get("ARENA17_MAX_UPLOAD_BYTES", 8 * 1024**3)),
        SERVE_DOWNLOADS_LOCALLY=os.environ.get("ARENA17_SERVE_DOWNLOADS_LOCALLY", "").lower() in {"1", "true", "yes"},
        ALLOWED_EXTENSIONS={"iso", "rom", "chd", "zip", "7z", "rar", "ips", "ppf", "xdelta", "bin"},
        LIVE_DIR=os.environ.get("ARENA17_LIVE_DIR", str(root / "storage" / "live")),
        SPECTATE_API_KEY=os.environ.get("ARENA17_SPECTATE_KEY", ""),
    )
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["DOWNLOADS_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_TMP_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["LIVE_DIR"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    from .security import init_template_helpers
    init_template_helpers(app)
    from .storage import human_size
    app.jinja_env.filters["filesize"] = human_size

    from .public import bp as public_bp
    from .admin import bp as admin_bp
    from .spectate import bp as spectate_bp
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(spectate_bp)
    return app

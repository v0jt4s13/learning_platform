from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, redirect, url_for
from flask_login import current_user

from .extensions import db, login_manager


def create_app() -> Flask:
    project_root = Path(__file__).resolve().parent.parent
    data_settings = Path("/home/vs/projects/ai/data_settings/.env")
    if data_settings.exists():
        load_dotenv(data_settings)
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    app = Flask(__name__)
    default_db_path = project_root / "learning_platform.db"
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", os.getenv("DB_URL", f"sqlite:///{default_db_path}"))
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    secret = os.getenv("FLASK_SECRET_KEY", "dev")
    app.config["SECRET_KEY"] = secret

    app.config["SESSION_COOKIE_NAME"] = os.getenv("SESSION_COOKIE_NAME", "learning_platform_session")
    app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}

    translation_provider = os.getenv("TRANSLATION_PROVIDER")
    if not translation_provider:
        if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
            translation_provider = "aws"
        else:
            translation_provider = "mock"

    app.config.setdefault("TRANSLATION_PROVIDER", translation_provider)
    app.config.setdefault("AWS_TRANSLATE_REGION", os.getenv("AWS_TRANSLATE_REGION") or os.getenv("S3_REGION"))
    app.config.setdefault("AZURE_SPEECH_KEY", os.getenv("AZURE_SPEECH_KEY"))
    app.config.setdefault("AZURE_REGION", os.getenv("AZURE_REGION"))
    app.config.setdefault("AZURE_SPEECH_VOICES", {
        "pl": os.getenv("AZURE_VOICE_PL", "pl-PL-AgnieszkaNeural"),
        "en": os.getenv("AZURE_VOICE_EN", "en-GB-MiaNeural"),
        "de": os.getenv("AZURE_VOICE_DE", "de-DE-MajaNeural"),
    })
    app.config.setdefault("S3_BUCKET", os.getenv("S3_BUCKET"))
    app.config.setdefault("S3_REGION", os.getenv("S3_REGION"))
    app.config.setdefault("S3_BASE_URL", os.getenv("S3_BASE_URL"))
    app.config.setdefault("S3_LEARNING_PREFIX", os.getenv("S3_LEARNING_PREFIX", "sentence-trainer"))

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    from .models import StudentAccount  # noqa: WPS433

    @login_manager.user_loader
    def _load_user(user_id: str) -> StudentAccount | None:
        if not user_id:
            return None
        try:
            student_id = int(user_id)
        except ValueError:
            return None
        return StudentAccount.query.get(student_id)

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("sentences.list_sentences"))
        return redirect(url_for("auth.login"))

    from .auth.routes import auth_bp
    from .sentences.routes import sentences_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(sentences_bp)

    with app.app_context():
        db.create_all()

    return app

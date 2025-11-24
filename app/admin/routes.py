from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..models import AppSetting
from ..services.storage import LocalStorage, S3Storage, build_storage
from ..services.translation import (
    AWSTranslateService,
    AzureTextToSpeechService,
    MockTextToSpeechService,
    MockTranslationService,
    OpenAITranslationService,
    configured_tts_voices,
    list_azure_voices,
    build_translation_service,
    build_tts_service,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _require_admin() -> None:
    if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
        abort(403)


def _backend_name(obj: Any) -> str:
    return obj.__class__.__name__


@dataclass
class TranslationDiagnostics:
    configured_provider: str
    backend: str
    aws_credentials_present: bool
    aws_region: str | None
    missing_env: list[str]
    note: str | None = None


@dataclass
class TtsDiagnostics:
    configured_provider: str
    backend: str
    azure_key_present: bool
    azure_region: str | None
    google_credentials_present: bool
    google_project: str | None
    missing_env: list[str]
    note: str | None = None


@dataclass
class StorageDiagnostics:
    backend: str
    bucket: str | None = None
    base_url: str | None = None
    region: str | None = None
    base_dir: str | None = None
    note: str | None = None


@admin_bp.route("/diagnostics", methods=["GET"])
@login_required
def diagnostics():
    _require_admin()

    config = current_app.config
    aws_key = bool(config.get("AWS_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID") or current_app.config.get("S3_BUCKET"))
    aws_secret = bool(config.get("AWS_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY"))
    aws_region = config.get("AWS_TRANSLATE_REGION") or config.get("S3_REGION") or os.getenv("AWS_TRANSLATE_REGION") or os.getenv("S3_REGION")
    aws_missing = []
    if not aws_key:
        aws_missing.append("AWS_ACCESS_KEY_ID")
    if not aws_secret:
        aws_missing.append("AWS_SECRET_ACCESS_KEY")
    if not aws_region:
        aws_missing.append("AWS_TRANSLATE_REGION")

    # Translation backend (with fallback awareness)
    translation_backend = build_translation_service(current_app)
    provider = (AppSetting.get("translation_provider") or config.get("TRANSLATION_PROVIDER") or "mock").lower()
    translation_note = None
    if isinstance(translation_backend, MockTranslationService):
        translation_note = "Używany jest tłumacz mock (brak/niepoprawne klucze lub wybrany mock)."
    translation_info = TranslationDiagnostics(
        configured_provider=provider,
        backend=_backend_name(translation_backend),
        aws_credentials_present=aws_key and aws_secret,
        aws_region=aws_region,
        note=translation_note,
        missing_env=aws_missing,
    )

    # TTS backend
    tts_provider = (
        AppSetting.get("tts_provider")
        or ("azure" if (config.get("AZURE_SPEECH_KEY") or os.getenv("AZURE_SPEECH_KEY")) else None)
        or "mock"
    )
    tts_backend = build_tts_service(current_app)
    tts_note = None
    if isinstance(tts_backend, MockTextToSpeechService):
        tts_note = "Używany jest TTS mock (brak/niepoprawne klucze)."
    tts_missing = []
    if tts_provider == "azure":
        if not config.get("AZURE_SPEECH_KEY") and not os.getenv("AZURE_SPEECH_KEY"):
            tts_missing.append("AZURE_SPEECH_KEY")
        if not config.get("AZURE_REGION") and not os.getenv("AZURE_REGION"):
            tts_missing.append("AZURE_REGION")
    if tts_provider == "google":
        if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS") and not config.get("GOOGLE_APPLICATION_CREDENTIALS"):
            tts_missing.append("GOOGLE_APPLICATION_CREDENTIALS")
        if not os.getenv("GCS_BUCKET") and not config.get("GCS_BUCKET"):
            tts_missing.append("GCS_BUCKET")
        if not os.getenv("GCS_PREFIX") and not config.get("GCS_PREFIX"):
            tts_missing.append("GCS_PREFIX")
        if not os.getenv("GOOGLE_CLOUD_PROJECT") and not config.get("GOOGLE_CLOUD_PROJECT"):
            tts_missing.append("GOOGLE_CLOUD_PROJECT")

    tts_info = TtsDiagnostics(
        configured_provider=tts_provider,
        backend=_backend_name(tts_backend),
        azure_key_present=bool(config.get("AZURE_SPEECH_KEY") or os.getenv("AZURE_SPEECH_KEY")),
        azure_region=config.get("AZURE_REGION") or os.getenv("AZURE_REGION"),
        google_credentials_present=bool(
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or config.get("GOOGLE_APPLICATION_CREDENTIALS")
        ),
        google_project=os.getenv("GOOGLE_CLOUD_PROJECT") or config.get("GOOGLE_CLOUD_PROJECT"),
        missing_env=tts_missing,
        note=tts_note,
    )

    # Storage backend
    storage_note = None
    storage_details: StorageDiagnostics
    try:
        storage_backend = build_storage(current_app)
        if isinstance(storage_backend, S3Storage):
            storage_details = StorageDiagnostics(
                backend=_backend_name(storage_backend),
                bucket=storage_backend.bucket,
                base_url=storage_backend.base_url or None,
                region=storage_backend.region or None,
            )
        elif isinstance(storage_backend, LocalStorage):
            storage_details = StorageDiagnostics(
                backend=_backend_name(storage_backend),
                base_dir=str(storage_backend.base_dir),
                note="Lokalny zapis audio — pliki nie trafiają do S3.",
            )
        else:  # pragma: no cover - defensive
            storage_details = StorageDiagnostics(
                backend=_backend_name(storage_backend),
                note="Nieznany typ storage.",
            )
    except Exception as exc:  # pragma: no cover - diagnostyka
        storage_details = StorageDiagnostics(
            backend="error",
            note=f"Storage niedostępny: {exc}",
        )

    selected_tts_voice = configured_tts_voices(current_app, tts_provider)
    azure_voices = list_azure_voices()
    azure_grouped: dict[str, list[dict]] = {lang: [] for lang in ("pl", "en", "de")}
    for voice in azure_voices:
        locale = (voice.get("Locale") or "").lower()
        # EN ograniczamy do en-GB, DE do de-DE
        if locale.startswith("en-gb"):
            azure_grouped["en"].append(voice)
        elif locale.startswith("de-de"):
            azure_grouped["de"].append(voice)
        elif locale.startswith("pl-"):
            azure_grouped["pl"].append(voice)
    for lang in azure_grouped:
        azure_grouped[lang].sort(key=lambda v: (v.get("DisplayName") or "").lower())

    return render_template(
        "admin/diagnostics.html",
        translation=asdict(translation_info),
        tts=asdict(tts_info),
        storage=asdict(storage_details),
        selected_provider=AppSetting.get("translation_provider") or config.get("TRANSLATION_PROVIDER") or "mock",
        selected_tts_provider=tts_provider,
        azure_voices=azure_grouped,
        selected_tts_voice=selected_tts_voice,
    )


@admin_bp.route("/translation-provider", methods=["POST"])
@login_required
def set_translation_provider():
    _require_admin()
    choice = (request.form.get("provider") or "").strip().lower()
    allowed = {"aws", "openai", "mock"}
    if choice not in allowed:
        flash("Nieprawidłowy provider tłumaczeń.", "error")
        return redirect(url_for("admin.diagnostics"))
    AppSetting.set("translation_provider", choice)
    flash(f"Ustawiono domyślny provider tłumaczeń: {choice}", "success")
    return redirect(url_for("admin.diagnostics"))


@admin_bp.route("/tts-provider", methods=["POST"])
@login_required
def set_tts_provider():
    _require_admin()
    choice = (request.form.get("provider") or "").strip().lower()
    allowed = {"azure", "google", "mock"}
    if choice not in allowed:
        flash("Nieprawidłowy provider TTS.", "error")
        return redirect(url_for("admin.diagnostics"))
    AppSetting.set("tts_provider", choice)
    flash(f"Ustawiono domyślny provider TTS: {choice}", "success")
    return redirect(url_for("admin.diagnostics"))


@admin_bp.route("/tts-voice", methods=["POST"])
@login_required
def set_tts_voice():
    _require_admin()
    provider = (request.form.get("provider") or "").strip().lower()
    language = (request.form.get("language") or "").strip().lower()
    voice = (request.form.get("voice") or "").strip()
    if provider not in {"azure", "google"} or language not in {"pl", "en", "de"}:
        flash("Nieprawidłowy provider lub język dla lektora.", "error")
        return redirect(url_for("admin.diagnostics"))

    # Google: walidacja prefiksu wg języka
    if provider == "google" and voice:
        lowered = voice.lower()
        if language == "en" and not lowered.startswith("en-gb"):
            flash("Dla Google TTS (EN) dozwolone są głosy en-GB.*", "error")
            return redirect(url_for("admin.diagnostics"))
        if language == "de" and not lowered.startswith("de-de"):
            flash("Dla Google TTS (DE) dozwolone są głosy de-DE.*", "error")
            return redirect(url_for("admin.diagnostics"))
        if language == "pl" and not lowered.startswith("pl-"):
            flash("Dla Google TTS (PL) dozwolone są głosy pl-*.", "error")
            return redirect(url_for("admin.diagnostics"))

    key = f"tts_voice_{provider}_{language}"
    AppSetting.set(key, voice or None)
    flash(f"Ustawiono lektora dla {provider.upper()} / {language.upper()}: {voice or 'domyślny'}", "success")
    return redirect(url_for("admin.diagnostics"))

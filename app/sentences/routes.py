from __future__ import annotations

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for, current_app
from flask_login import current_user, login_required

from ..models import DifficultyLevel, LANGUAGE_CHOICES, Sentence, StudentAccount
from ..services.sentences import SentenceTrainerService
from ..services.shared_sentences import SharedSentenceService
from ..services.translation import (
    SentenceProcessingError,
    SentenceValidationError,
    determine_target_languages,
    list_azure_voices,
)

sentences_bp = Blueprint("sentences", __name__)


def _require_student() -> StudentAccount:
    if not isinstance(current_user, StudentAccount):  # pragma: no cover - defensive
        raise SentenceValidationError("Musisz być zalogowanym uczniem.")
    return current_user


@sentences_bp.route("/sentences")
@login_required
def list_sentences():
    student = _require_student()
    source_language = request.args.get("source_language") or None
    if source_language and source_language not in LANGUAGE_CHOICES:
        source_language = None
    search = request.args.get("q") or None
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except ValueError:
        page = 1
    service = SentenceTrainerService()
    pagination = service.list_sentences(
        student,
        source_language=source_language,
        search=search,
        page=page,
        per_page=20,
    )
    return render_template(
        "sentences/list.html",
        sentences=pagination.items,
        pagination=pagination,
        languages=LANGUAGE_CHOICES,
        filters={"source_language": source_language, "q": search},
    )


@sentences_bp.route("/sentences/new", methods=["GET", "POST"])
@login_required
def create_sentence():
    student = _require_student()
    service = SentenceTrainerService()
    created_sentence = None
    error = None
    if request.method == "POST":
        source_text = request.form.get("source_text") or ""
        source_language = request.form.get("source_language") or "pl"
        try:
            created_sentence = service.create_sentence(student, source_text, source_language)
        except (SentenceValidationError, SentenceProcessingError) as exc:
            error = str(exc)
    try:
        targets = determine_target_languages(request.form.get("source_language") or "pl")
    except SentenceValidationError:
        targets = determine_target_languages("pl")
    return render_template(
        "sentences/form.html",
        languages=LANGUAGE_CHOICES,
        targets=targets,
        created_sentence=created_sentence,
        error=error,
    )


@sentences_bp.route("/sentences/<int:sentence_id>/delete", methods=["POST"])
@login_required
def delete_sentence(sentence_id: int):
    student = _require_student()
    service = SentenceTrainerService()
    if service.delete_sentence(student, sentence_id):
        flash("Zdanie zostało usunięte.", "success")
    else:
        flash("Nie znaleziono zdania.", "error")
    return redirect(url_for("sentences.list_sentences"))


@sentences_bp.route("/api/sentences", methods=["GET"])
@login_required
def api_list_sentences():
    student = _require_student()
    source_language = request.args.get("source_language") or None
    if source_language and source_language not in LANGUAGE_CHOICES:
        source_language = None
    search = request.args.get("q") or None
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = max(1, min(int(request.args.get("per_page", 20)), 50))
    except ValueError:
        return jsonify({"error": "Parametry paginacji muszą być liczbami."}), 400
    service = SentenceTrainerService()
    pagination = service.list_sentences(
        student,
        source_language=source_language,
        search=search,
        page=page,
        per_page=per_page,
    )
    return jsonify(
        {
            "items": [service.serialize(sentence) for sentence in pagination.items],
            "pagination": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            },
        }
    )


@sentences_bp.route("/api/sentences", methods=["POST"])
@login_required
def api_create_sentence():
    student = _require_student()
    payload = request.get_json(silent=True) or {}
    source_text = payload.get("source_text") or ""
    source_language = payload.get("source_language") or "pl"
    service = SentenceTrainerService()
    try:
        sentence = service.create_sentence(student, source_text, source_language)
        return jsonify(service.serialize(sentence)), 201
    except SentenceValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except SentenceProcessingError as exc:
        return jsonify({"error": str(exc)}), 500


@sentences_bp.route("/api/sentences/<int:sentence_id>", methods=["GET"])
@login_required
def api_sentence_detail(sentence_id: int):
    student = _require_student()
    sentence = Sentence.query.filter_by(id=sentence_id, user_id=student.id).first()
    if not sentence:
        return jsonify({"error": "Nie znaleziono zdania."}), 404
    return jsonify(SentenceTrainerService().serialize(sentence))


@sentences_bp.route("/api/sentences/<int:sentence_id>", methods=["DELETE"])
@login_required
def api_delete_sentence(sentence_id: int):
    student = _require_student()
    service = SentenceTrainerService()
    if not service.delete_sentence(student, sentence_id):
        return jsonify({"error": "Nie znaleziono zdania."}), 404
    return "", 204


@sentences_bp.route("/shared", methods=["GET"])
@login_required
def shared_sentences():
    _require_student()
    difficulty = request.args.get("difficulty") or None
    if difficulty and difficulty not in DifficultyLevel.values():
        difficulty = None
    search = request.args.get("q") or None
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except ValueError:
        page = 1

    service = SharedSentenceService()
    pagination = service.list_shared(
        difficulty=difficulty,
        search=search,
        page=page,
        per_page=50,
        only_translated=True,
    )

    grouped = {level: [] for level in DifficultyLevel.values()}
    for sentence in pagination.items:
        grouped.get(sentence.difficulty, []).append(sentence)

    return render_template(
        "sentences/shared_list.html",
        grouped=grouped,
        pagination=pagination,
        filters={"difficulty": difficulty, "q": search},
    )


@sentences_bp.route("/voices", methods=["GET"])
@login_required
def list_voices():
    _require_student()
    available = list_azure_voices()
    grouped: dict[str, list[dict]] = {lang: [] for lang in LANGUAGE_CHOICES}
    for voice in available:
        locale = (voice.get("Locale") or "").lower()
        for lang in LANGUAGE_CHOICES:
            if locale.startswith(lang.lower()):
                grouped[lang].append(voice)
    for lang in grouped:
        grouped[lang].sort(key=lambda v: (v.get("DisplayName") or "").lower())
    return render_template("sentences/voices.html", grouped=grouped)

from __future__ import annotations

from dataclasses import dataclass

from flask import current_app
from sqlalchemy import or_

from ..extensions import db
from ..models import LANGUAGE_CHOICES, DifficultyLevel, SharedSentence
from .storage import StorageBackend, build_storage
from .translation import (
    MockTextToSpeechService,
    SentenceProcessingError,
    SentenceValidationError,
    build_translation_service,
    build_tts_service,
    determine_target_languages,
    provider_info,
    tts_voice_label,
    validate_language_selection,
)


def shared_storage() -> StorageBackend:
    return build_storage(current_app)


@dataclass
class SharedPagination:
    items: list[SharedSentence]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        if self.per_page <= 0:
            return 1
        return max((self.total + self.per_page - 1) // self.per_page, 1)


class SharedSentenceService:
    def __init__(self, storage: StorageBackend | None = None, translator=None, tts: MockTextToSpeechService | None = None) -> None:
        self.storage = storage or shared_storage()
        self.translator = translator or build_translation_service(current_app)
        self.tts = tts or build_tts_service(current_app)

    def _prefix(self) -> str:
        prefix = current_app.config.get("S3_LEARNING_PREFIX", "sentence-trainer") or "sentence-trainer"
        prefix = prefix.strip("/") or "sentence-trainer"
        return f"{prefix}/shared"

    def _audio_key(self, sentence_id: int, language: str) -> str:
        return f"{self._prefix()}/{sentence_id}/{language}.mp3"

    def list_shared(
        self,
        *,
        difficulty: str | None = None,
        search: str | None = None,
        page: int = 1,
        per_page: int = 20,
        only_translated: bool = True,
    ) -> SharedPagination:
        per_page = max(1, min(per_page, 100))
        page = max(1, page)
        query = SharedSentence.query
        if only_translated:
            query = query.filter(SharedSentence.status == "translated")
        if difficulty and difficulty in DifficultyLevel.values():
            query = query.filter(SharedSentence.difficulty == difficulty)
        if search:
            like = f"%{search.lower()}%"
            query = query.filter(
                or_(
                    SharedSentence.source_text.ilike(like),
                    SharedSentence.translated_text_1.ilike(like),
                    SharedSentence.translated_text_2.ilike(like),
                    SharedSentence.prompt.ilike(like),
                )
            )
        total = query.count()
        rows = (
            query.order_by(SharedSentence.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return SharedPagination(rows, total, page, per_page)

    def create_from_prompt(self, prompt: str, difficulty: str, source_language: str, texts: list[str], created_by: int | None = None) -> list[SharedSentence]:
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise SentenceValidationError("Prompt nie może być pusty.")
        if difficulty not in DifficultyLevel.values():
            raise SentenceValidationError("Nieprawidłowy poziom trudności.")
        if source_language not in LANGUAGE_CHOICES:
            raise SentenceValidationError("Nieobsługiwany język źródłowy.")
        target_one, target_two = determine_target_languages(source_language)
        validate_language_selection(source_language, target_one, target_two)

        created: list[SharedSentence] = []
        for raw in texts:
            cleaned_text = (raw or "").strip()
            if not cleaned_text:
                continue
            shared = SharedSentence(
                prompt=clean_prompt,
                difficulty=difficulty,
                source_language=source_language,
                source_text=cleaned_text,
                target_language_1=target_one,
                target_language_2=target_two,
                status="draft",
                created_by=created_by,
            )
            db.session.add(shared)
            created.append(shared)
        db.session.commit()
        return created

    def translate(self, shared: SharedSentence) -> SharedSentence:
        if not shared:
            raise SentenceValidationError("Brak zdania do tłumaczenia.")

        target_one, target_two = shared.target_language_1, shared.target_language_2
        validate_language_selection(shared.source_language, target_one, target_two)

        providers = provider_info(current_app)
        translated_one = self.translator.translate(shared.source_text, shared.source_language, target_one)
        translated_two = self.translator.translate(shared.source_text, shared.source_language, target_two)

        voice_source = tts_voice_label(self.tts, shared.source_language)
        voice_one = tts_voice_label(self.tts, target_one)
        voice_two = tts_voice_label(self.tts, target_two)

        shared.translated_text_1 = translated_one
        shared.translated_text_2 = translated_two
        shared.translation_provider = providers.get("translation_provider")
        shared.tts_provider = providers.get("tts_provider")
        shared.tts_voice_source = voice_source
        shared.tts_voice_1 = voice_one
        shared.tts_voice_2 = voice_two

        try:
            audio_source = self.tts.synthesize(shared.source_text, shared.source_language)
            audio_one = self.tts.synthesize(translated_one, target_one)
            audio_two = self.tts.synthesize(translated_two, target_two)
            shared.audio_url_source = self.storage.upload_audio(audio_source, self._audio_key(shared.id, shared.source_language))
            shared.audio_url_1 = self.storage.upload_audio(audio_one, self._audio_key(shared.id, target_one))
            shared.audio_url_2 = self.storage.upload_audio(audio_two, self._audio_key(shared.id, target_two))
        except SentenceProcessingError:
            db.session.rollback()
            raise
        except Exception as exc:  # pragma: no cover - unexpected path
            db.session.rollback()
            raise SentenceProcessingError("Nie udało się wygenerować nagrań audio.") from exc

        shared.status = "translated"
        shared.touch()
        db.session.add(shared)  # ensure attached
        db.session.commit()
        return shared

    def delete(self, sentence_id: int) -> bool:
        shared = SharedSentence.query.filter_by(id=sentence_id).first()
        if not shared:
            return False

        languages = [shared.source_language, shared.target_language_1, shared.target_language_2]
        for lang in languages:
            if not lang:
                continue
            key = self._audio_key(shared.id, lang)
            try:
                self.storage.delete_audio(key)
            except SentenceProcessingError as exc:  # pragma: no cover - logging
                current_app.logger.warning("Nie udało się usunąć pliku %s: %s", key, exc)

        db.session.delete(shared)
        db.session.commit()
        return True

    def serialize(self, sentence: SharedSentence) -> dict:
        return sentence.as_dict()

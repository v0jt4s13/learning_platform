from __future__ import annotations

from dataclasses import dataclass

from flask import current_app
from sqlalchemy import or_

from ..extensions import db
from ..models import Sentence, StudentAccount
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


def sentence_storage() -> StorageBackend:
    return build_storage(current_app)


@dataclass
class Pagination:
    items: list[Sentence]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        if self.per_page <= 0:
            return 1
        return max((self.total + self.per_page - 1) // self.per_page, 1)


class SentenceTrainerService:
    def __init__(
        self,
        storage: StorageBackend | None = None,
        translator=None,
        tts: MockTextToSpeechService | None = None,
    ) -> None:
        self.storage = storage or sentence_storage()
        self.translator = translator or build_translation_service(current_app)
        self.tts = tts or build_tts_service(current_app)

    def _prefix(self) -> str:
        prefix = current_app.config.get("S3_LEARNING_PREFIX", "sentence-trainer") or "sentence-trainer"
        prefix = prefix.strip("/") or "sentence-trainer"
        return prefix

    def _audio_key(self, student_id: int, sentence_id: int, language: str) -> str:
        return f"{self._prefix()}/{student_id}/{sentence_id}/{language}.mp3"

    def list_sentences(
        self,
        student: StudentAccount,
        *,
        source_language: str | None = None,
        search: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> Pagination:
        per_page = max(1, min(per_page, 50))
        page = max(1, page)
        query = Sentence.query.filter_by(user_id=student.id)
        if source_language:
            query = query.filter(Sentence.source_language == source_language)
        if search:
            like = f"%{search.lower()}%"
            query = query.filter(
                or_(
                    Sentence.source_text.ilike(like),
                    Sentence.translated_text_1.ilike(like),
                    Sentence.translated_text_2.ilike(like),
                )
            )
        total = query.count()
        rows = (
            query.order_by(Sentence.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return Pagination(rows, total, page, per_page)

    def create_sentence(self, student: StudentAccount, source_text: str, source_language: str) -> Sentence:
        cleaned_text = (source_text or "").strip()
        if not cleaned_text:
            raise SentenceValidationError("Zdanie do nauki nie może być puste.")
        target_one, target_two = determine_target_languages(source_language)
        validate_language_selection(source_language, target_one, target_two)

        providers = provider_info(current_app)

        translated_one = self.translator.translate(cleaned_text, source_language, target_one)
        translated_two = self.translator.translate(cleaned_text, source_language, target_two)

        voice_source = tts_voice_label(self.tts, source_language)
        voice_one = tts_voice_label(self.tts, target_one)
        voice_two = tts_voice_label(self.tts, target_two)

        sentence = Sentence(
            student=student,
            source_language=source_language,
            source_text=cleaned_text,
            target_language_1=target_one,
            target_language_2=target_two,
            translated_text_1=translated_one,
            translated_text_2=translated_two,
            translation_provider=providers.get("translation_provider"),
            tts_provider=providers.get("tts_provider"),
            tts_voice_source=voice_source,
            tts_voice_1=voice_one,
            tts_voice_2=voice_two,
        )
        db.session.add(sentence)
        db.session.flush()

        try:
            audio_source = self.tts.synthesize(cleaned_text, source_language)
            audio_one = self.tts.synthesize(translated_one, target_one)
            audio_two = self.tts.synthesize(translated_two, target_two)
            sentence.audio_url_source = self.storage.upload_audio(
                audio_source,
                self._audio_key(student.id, sentence.id, source_language),
            )
            sentence.audio_url_1 = self.storage.upload_audio(
                audio_one,
                self._audio_key(student.id, sentence.id, target_one),
            )
            sentence.audio_url_2 = self.storage.upload_audio(
                audio_two,
                self._audio_key(student.id, sentence.id, target_two),
            )
        except SentenceProcessingError:
            db.session.rollback()
            raise
        except Exception as exc:  # pragma: no cover - unexpected errors bubble up
            db.session.rollback()
            raise SentenceProcessingError("Nie udało się wygenerować nagrań audio.") from exc

        sentence.touch()
        db.session.commit()
        return sentence

    def delete_sentence(self, student: StudentAccount, sentence_id: int) -> bool:
        sentence = Sentence.query.filter_by(id=sentence_id, user_id=student.id).first()
        if not sentence:
            return False

        languages = [
            sentence.source_language,
            sentence.target_language_1,
            sentence.target_language_2,
        ]

        for language in languages:
            if not language:
                continue
            key = self._audio_key(student.id, sentence.id, language)
            try:
                self.storage.delete_audio(key)
            except SentenceProcessingError as exc:  # pragma: no cover - logging
                current_app.logger.warning("Nie udało się usunąć pliku %s: %s", key, exc)

        db.session.delete(sentence)
        db.session.commit()
        return True

    def serialize(self, sentence: Sentence) -> dict:
        return sentence.as_dict()

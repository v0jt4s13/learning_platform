from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ["DB_URL"] = "sqlite:///:memory:"

from app import create_app  # noqa: E402  - loaded after env override
from app.extensions import db  # noqa: E402
from app.models import Sentence, SharedSentence, StudentAccount  # noqa: E402
from app.services.sentences import SentenceTrainerService  # noqa: E402
from app.services.shared_sentences import SharedSentenceService  # noqa: E402
from app.services.generator import SentenceGenerationService, SentenceGenerationError  # noqa: E402
from app.services.storage import LocalStorage  # noqa: E402
from app.services.translation import (  # noqa: E402
    MockTextToSpeechService,
    MockTranslationService,
    SentenceValidationError,
    determine_target_languages,
    validate_language_selection,
)


@pytest.fixture()
def test_app():
    app = create_app()
    app.config.update(TESTING=True, TRANSLATION_PROVIDER="mock")
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield app


def test_determine_target_languages_returns_remaining_codes():
    assert determine_target_languages("pl") == ("en", "de")
    assert determine_target_languages("en") == ("pl", "de")


def test_validate_language_selection_rejects_duplicates():
    validate_language_selection("pl", "en", "de")
    with pytest.raises(SentenceValidationError):
        validate_language_selection("pl", "pl", "de")


def test_sentence_creation_persists_user_binding(test_app):
    with test_app.app_context():
        student = StudentAccount(username="tester")
        student.set_password("haslo1234")
        db.session.add(student)
        db.session.commit()

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SentenceTrainerService(
                storage=LocalStorage(Path(tmpdir), "/files"),
                translator=MockTranslationService(),
                tts=MockTextToSpeechService(),
            )
            sentence = service.create_sentence(student, "To jest zdanie testowe", "pl")
            assert sentence.user_id == student.id
            assert sentence.audio_url_source.startswith("/files/")
            assert sentence.target_language_1 != sentence.target_language_2


def test_delete_sentence_removes_audio_and_row(test_app):
    with test_app.app_context():
        student = StudentAccount(username="deleter")
        student.set_password("bezpiecznehaslo")
        db.session.add(student)
        db.session.commit()

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = LocalStorage(Path(tmpdir), "/files")
            service = SentenceTrainerService(
                storage=storage,
                translator=MockTranslationService(),
                tts=MockTextToSpeechService(),
            )
            sentence = service.create_sentence(student, "Jutro będzie zupa z dyni.", "pl")

            base_dir = Path(tmpdir)
            key_pl = service._audio_key(student.id, sentence.id, "pl")
            key_en = service._audio_key(student.id, sentence.id, "en")
            key_de = service._audio_key(student.id, sentence.id, "de")
            assert (base_dir / key_pl).exists()
            assert (base_dir / key_en).exists()
            assert (base_dir / key_de).exists()

            assert service.delete_sentence(student, sentence.id) is True
            assert not (base_dir / key_pl).exists()
            assert not (base_dir / key_en).exists()
            assert not (base_dir / key_de).exists()
            assert Sentence.query.filter_by(id=sentence.id).first() is None


def test_shared_sentence_translation_flow(test_app):
    with test_app.app_context():
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = LocalStorage(Path(tmpdir), "/files")
            service = SharedSentenceService(
                storage=storage,
                translator=MockTranslationService(),
                tts=MockTextToSpeechService(),
            )

            created = service.create_from_prompt(
                prompt="Test prompt",
                difficulty="beginner",
                source_language="pl",
                texts=["To jest zdanie wspólne"],
            )
            shared = created[0]

            translated = service.translate(shared)

            assert translated.status == "translated"
            assert translated.translated_text_1
            assert translated.translated_text_2

            base_dir = Path(tmpdir)
            key_pl = service._audio_key(translated.id, "pl")
            key_en = service._audio_key(translated.id, "en")
            key_de = service._audio_key(translated.id, "de")
            assert (base_dir / key_pl).exists()
            assert (base_dir / key_en).exists()
            assert (base_dir / key_de).exists()
            assert SharedSentence.query.filter_by(id=translated.id).first() is not None


def test_generator_parses_common_json_shapes(test_app):
    with test_app.app_context():
        svc = SentenceGenerationService()

        # list root
        sentences = svc._parse_content('["Ala ma kota", {"text": "Ala ma psa"}]')
        assert len(sentences) == 2

        # object with sentences key
        sentences = svc._parse_content('{"sentences": ["Tekst 1", {"sentence": "Tekst 2"}]}')
        assert len(sentences) == 2

        # object with lista key (PL)
        sentences = svc._parse_content('{"lista": ["Zdanie 1", "Zdanie 2"]}')
        assert len(sentences) == 2

        with pytest.raises(SentenceGenerationError):
            svc._parse_content('{"note": "brak listy"}')


def test_generator_fallback_contains_raw_response(test_app):
    with test_app.app_context():
        svc = SentenceGenerationService()
        result = svc.fallback.generate("prompt test")
        assert result.raw_response

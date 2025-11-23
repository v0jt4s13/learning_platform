from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ["DB_URL"] = "sqlite:///:memory:"

from app import create_app  # noqa: E402  - loaded after env override
from app.extensions import db  # noqa: E402
from app.models import Sentence, StudentAccount  # noqa: E402
from app.services.sentences import SentenceTrainerService  # noqa: E402
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

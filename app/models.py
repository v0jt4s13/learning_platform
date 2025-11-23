from __future__ import annotations

import datetime as dt
from enum import Enum

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, Index
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


class LanguageCode(str, Enum):
    PL = "pl"
    EN = "en"
    DE = "de"

    @classmethod
    def values(cls) -> tuple[str, str, str]:
        return cls.PL.value, cls.EN.value, cls.DE.value


LANGUAGE_CHOICES = LanguageCode.values()


def language_enum() -> db.Enum:
    return db.Enum(*LANGUAGE_CHOICES, name="language_code")


class StudentAccount(db.Model, UserMixin):
    __tablename__ = "lp_students"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)

    sentences = db.relationship("Sentence", back_populates="student", cascade="all, delete-orphan")

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        if not raw_password:
            return False
        return check_password_hash(self.password_hash, raw_password)

    def get_id(self) -> str:  # noqa: D401
        return str(self.id)


class Sentence(db.Model):
    __tablename__ = "lp_sentences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("lp_students.id", ondelete="CASCADE"), nullable=False)
    source_language = db.Column(language_enum(), nullable=False)
    source_text = db.Column(db.Text, nullable=False)
    target_language_1 = db.Column(language_enum(), nullable=False)
    target_language_2 = db.Column(language_enum(), nullable=False)
    translated_text_1 = db.Column(db.Text, nullable=False)
    translated_text_2 = db.Column(db.Text, nullable=False)
    audio_url_source = db.Column(db.String(512))
    audio_url_1 = db.Column(db.String(512))
    audio_url_2 = db.Column(db.String(512))
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)

    student = db.relationship("StudentAccount", back_populates="sentences")

    __table_args__ = (
        Index("ix_lp_sentences_user_created", "user_id", "created_at"),
        CheckConstraint("target_language_1 != target_language_2", name="ck_targets_unique"),
    )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "source_language": self.source_language,
            "source_text": self.source_text,
            "target_language_1": self.target_language_1,
            "target_language_2": self.target_language_2,
            "translated_text_1": self.translated_text_1,
            "translated_text_2": self.translated_text_2,
            "audio_url_source": self.audio_url_source,
            "audio_url_1": self.audio_url_1,
            "audio_url_2": self.audio_url_2,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def touch(self) -> None:
        self.updated_at = dt.datetime.utcnow()

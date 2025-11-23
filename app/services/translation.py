from __future__ import annotations

import html
import time
from dataclasses import dataclass
from typing import Protocol

import requests
from flask import current_app

from ..models import LANGUAGE_CHOICES


class SentenceValidationError(ValueError):
    """Raised when incoming data fails validation."""


class SentenceProcessingError(RuntimeError):
    """Raised when TTS or storage fails."""


class TranslationBackend(Protocol):
    def translate(self, text: str, source_language: str, target_language: str) -> str:  # pragma: no cover - interface
        ...


class TextToSpeechBackend(Protocol):
    def synthesize(self, text: str, language: str) -> bytes:  # pragma: no cover - interface
        ...


class MockTranslationService:
    def translate(self, text: str, source_language: str, target_language: str) -> str:
        normalized = (text or "").strip()
        if not normalized:
            raise SentenceValidationError("Zdanie do nauki nie może być puste.")
        return f"{normalized} ⇒ {target_language.upper()}"


class MockTextToSpeechService:
    def synthesize(self, text: str, language: str) -> bytes:
        payload = f"MOCK::{language}::{text.strip()}"
        return payload.encode("utf-8")


class AWSTranslateService:
    def __init__(self, region_name: str | None = None) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise SentenceProcessingError("Wymagany jest pakiet boto3 do tłumaczeń AWS.") from exc
        self.client = boto3.client("translate", region_name=region_name)

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        normalized = (text or "").strip()
        if not normalized:
            raise SentenceValidationError("Zdanie do nauki nie może być puste.")
        if source_language == target_language:
            return normalized
        try:
            response = self.client.translate_text(
                Text=normalized,
                SourceLanguageCode=source_language,
                TargetLanguageCode=target_language,
            )
        except Exception as exc:  # pragma: no cover - depends on AWS connectivity
            raise SentenceProcessingError("Nie udało się wykonać tłumaczenia w AWS.") from exc
        return response.get("TranslatedText", normalized)


class AzureTextToSpeechService:
    DEFAULT_VOICES = {
        "pl": "pl-PL-ZofiaNeural",
        "en": "en-US-AriaNeural",
        "de": "de-DE-KatjaNeural",
    }

    LANG_TAGS = {
        "pl": "pl-PL",
        "en": "en-US",
        "de": "de-DE",
    }

    def __init__(self, key: str, region: str, voices: dict[str, str] | None = None) -> None:
        if not key or not region:
            raise SentenceProcessingError("Brak konfiguracji Azure Speech.")
        self.key = key
        self.region = region
        self.voices = voices or self.DEFAULT_VOICES
        self._token: str | None = None
        self._token_expiry = 0.0

    def _issue_token(self) -> None:
        url = f"https://{self.region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
        response = requests.post(url, headers={"Ocp-Apim-Subscription-Key": self.key}, timeout=10)
        if response.status_code != 200:
            raise SentenceProcessingError("Nie udało się pobrać tokenu Azure TTS.")
        self._token = response.text
        self._token_expiry = time.time() + 540  # token ważny 10 minut

    def _ensure_token(self) -> str:
        if not self._token or time.time() >= self._token_expiry:
            self._issue_token()
        assert self._token  # nosec - ustawione powyżej
        return self._token

    def _voice_for(self, language: str) -> tuple[str, str]:
        voice = self.voices.get(language) or self.DEFAULT_VOICES.get(language) or self.DEFAULT_VOICES["en"]
        lang_tag = voice.split("-")
        if len(lang_tag) >= 2:
            lang = "-".join(lang_tag[:2])
        else:
            lang = self.LANG_TAGS.get(language, "en-US")
        return voice, lang

    def synthesize(self, text: str, language: str) -> bytes:
        cleaned = (text or "").strip()
        if not cleaned:
            raise SentenceValidationError("Tekst do wygenerowania audio nie może być pusty.")
        token = self._ensure_token()
        voice, lang = self._voice_for(language)
        url = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        ssml = (
            "<speak version='1.0' xml:lang='{lang}'>"
            "<voice xml:lang='{lang}' name='{voice}'>{text}</voice>"
            "</speak>"
        ).format(lang=lang, voice=voice, text=html.escape(cleaned))
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
            "User-Agent": "SentenceTrainer/1.0",
        }
        response = requests.post(url, headers=headers, data=ssml.encode("utf-8"), timeout=15)
        if response.status_code >= 400:
            raise SentenceProcessingError("Azure TTS zwrócił błąd.")
        return response.content


def determine_target_languages(source_language: str) -> tuple[str, str]:
    normalized = (source_language or "").strip().lower()
    if normalized not in LANGUAGE_CHOICES:
        raise SentenceValidationError("Nieobsługiwany język źródłowy.")
    targets = [lang for lang in LANGUAGE_CHOICES if lang != normalized]
    if len(targets) != 2:
        raise SentenceValidationError("Nie udało się określić języków docelowych.")
    return tuple(targets)  # type: ignore[return-value]


def validate_language_selection(source: str, target_one: str, target_two: str) -> None:
    trio = [source, target_one, target_two]
    invalid = [lang for lang in trio if lang not in LANGUAGE_CHOICES]
    if invalid:
        raise SentenceValidationError("Dozwolone języki to: pl, en, de.")
    if len(set(trio)) != 3:
        raise SentenceValidationError("Źródłowy i docelowe języki muszą być różne.")


def build_translation_service(app=None) -> TranslationBackend:
    app = app or current_app
    provider = (app.config.get("TRANSLATION_PROVIDER") or "mock").lower()
    if provider == "aws":
        region = app.config.get("AWS_TRANSLATE_REGION") or app.config.get("S3_REGION")
        try:
            return AWSTranslateService(region_name=region)
        except SentenceProcessingError as exc:
            app.logger.warning("AWS Translate niedostępny, używam tłumacza mock: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            app.logger.warning("Nieoczekiwany błąd AWS Translate, fallback do mock: %s", exc)
    return MockTranslationService()


def build_tts_service(app=None) -> TextToSpeechBackend:
    app = app or current_app
    key = app.config.get("AZURE_SPEECH_KEY")
    region = app.config.get("AZURE_REGION")
    voices = app.config.get("AZURE_SPEECH_VOICES")
    if key and region:
        try:
            return AzureTextToSpeechService(key=key, region=region, voices=voices)
        except SentenceProcessingError as exc:
            app.logger.warning("Azure TTS niedostępny, używam silnika mock: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            app.logger.warning("Nieoczekiwany błąd Azure TTS, fallback do mock: %s", exc)
    return MockTextToSpeechService()


_VOICE_CACHE: dict[str, object] = {"ts": 0.0, "voices": []}


def list_azure_voices(app=None) -> list[dict]:
    app = app or current_app
    key = app.config.get("AZURE_SPEECH_KEY")
    region = app.config.get("AZURE_REGION")
    if not key or not region:
        return []

    now = time.time()
    cached = _VOICE_CACHE.get("voices", [])
    if cached and now - (_VOICE_CACHE.get("ts") or 0) < 600:
        return cached  # type: ignore[return-value]

    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list"
    response = requests.get(url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=10)
    if response.status_code != 200:
        app.logger.warning("Nie udało się pobrać listy lektorów Azure (status %s)", response.status_code)
        return []
    try:
        voices = response.json()
    except Exception:  # pragma: no cover - fallback
        return []
    _VOICE_CACHE["voices"] = voices
    _VOICE_CACHE["ts"] = now
    return voices

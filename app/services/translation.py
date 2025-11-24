from __future__ import annotations

import html
import os
import time
from dataclasses import dataclass
from typing import Protocol

import requests
from flask import current_app

from ..models import LANGUAGE_CHOICES, AppSetting


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

    def voice_label(self, language: str) -> str:  # pragma: no cover - interface
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

    def voice_label(self, language: str) -> str:
        return "mock"


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


class OpenAITranslationService:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        if not api_key:
            raise SentenceProcessingError("Brak klucza OpenAI API.")
        self.api_key = api_key
        self.model = model
        self.endpoint = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions")

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        normalized = (text or "").strip()
        if not normalized:
            raise SentenceValidationError("Zdanie do nauki nie może być puste.")
        if source_language == target_language:
            return normalized

        system_prompt = (
            "Jesteś tłumaczem. Zwróć tylko tłumaczenie bez dodatkowych komentarzy. "
            "Zachowuj oryginalną interpunkcję, nie dodawaj nic od siebie."
        )
        user_prompt = f"Przetłumacz tekst z języka {source_language} na {target_language}: {normalized}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        try:
            response = requests.post(self.endpoint, json=payload, headers=headers, timeout=15)
        except Exception as exc:  # pragma: no cover - network issues
            raise SentenceProcessingError("Błąd połączenia z OpenAI.") from exc

        if response.status_code >= 400:
            raise SentenceProcessingError("OpenAI zwróciło błąd podczas tłumaczenia.")
        try:
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # pragma: no cover - unexpected payload
            raise SentenceProcessingError("Nie udało się odczytać odpowiedzi OpenAI.") from exc


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

    def __init__(
        self,
        key: str,
        region: str,
        voices: dict[str, str] | None = None,
        voice_overrides: dict[str, str] | None = None,
    ) -> None:
        if not key or not region:
            raise SentenceProcessingError("Brak konfiguracji Azure Speech.")
        self.key = key
        self.region = region
        self.voices = voices or self.DEFAULT_VOICES
        self.voice_overrides = voice_overrides or {}
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
        voice = self.voice_overrides.get(language) or self.voices.get(language) or self.DEFAULT_VOICES.get(language) or self.DEFAULT_VOICES["en"]
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

    def voice_label(self, language: str) -> str:
        voice, _ = self._voice_for(language)
        return voice


class GoogleTextToSpeechService:
    VOICE_DEFAULTS = {
        "pl": "pl-PL-Wavenet-E",
        "en": "en-US-Wavenet-D",
        "de": "de-DE-Wavenet-B",
    }

    def __init__(
        self,
        credentials_path: str | None = None,
        language_fallbacks: str | None = None,
        voice_overrides: dict[str, str] | None = None,
    ) -> None:
        try:
            from google.cloud import texttospeech as gtts  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency
            raise SentenceProcessingError("Wymagany jest pakiet google-cloud-texttospeech.") from exc

        self.gtts = gtts
        self.credentials_path = credentials_path
        self.language_fallbacks = [
            lang.strip() for lang in (language_fallbacks or "pl-PL,en-US,de-DE").split(",") if lang.strip()
        ]
        self.voice_overrides = voice_overrides or {}
        self._client = None

    def _client_instance(self):
        if self._client:
            return self._client
        if self.credentials_path:
            self._client = self.gtts.TextToSpeechClient.from_service_account_file(self.credentials_path)
        else:
            self._client = self.gtts.TextToSpeechClient()
        return self._client

    def _language_tag(self, language: str) -> str:
        mapping = {"pl": "pl-PL", "en": "en-US", "de": "de-DE"}
        return mapping.get(language, self.language_fallbacks[0] if self.language_fallbacks else "en-US")

    def _voice_name(self, language: str) -> str:
        return self.voice_overrides.get(language) or self.VOICE_DEFAULTS.get(language, self.VOICE_DEFAULTS["en"])

    def synthesize(self, text: str, language: str) -> bytes:
        cleaned = (text or "").strip()
        if not cleaned:
            raise SentenceValidationError("Tekst do wygenerowania audio nie może być pusty.")
        client = self._client_instance()
        lang_tag = self._language_tag(language)
        request = self.gtts.SynthesizeSpeechRequest(
            input=self.gtts.SynthesisInput(text=cleaned),
            voice=self.gtts.VoiceSelectionParams(
                language_code=lang_tag,
                name=self._voice_name(language),
                ssml_gender=self.gtts.SsmlVoiceGender.NEUTRAL,
            ),
            audio_config=self.gtts.AudioConfig(audio_encoding=self.gtts.AudioEncoding.MP3),
        )
        try:
            response = client.synthesize_speech(request=request)
        except Exception as exc:  # pragma: no cover - network/creds
            raise SentenceProcessingError("Google TTS zwróciło błąd.") from exc
        return response.audio_content

    def voice_label(self, language: str) -> str:
        return self._voice_name(language)


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


def _configured_provider(app) -> str:
    with app.app_context():
        db_choice = AppSetting.get("translation_provider")
    if db_choice:
        return db_choice.lower()
    return (app.config.get("TRANSLATION_PROVIDER") or "mock").lower()


def _configured_tts_provider(app) -> str:
    with app.app_context():
        db_choice = AppSetting.get("tts_provider")
    if db_choice:
        return db_choice.lower()
    # backward compatibility: use Azure when keys present
    if app.config.get("AZURE_SPEECH_KEY") or app.config.get("AZURE_REGION"):
        return "azure"
    if os.getenv("AZURE_SPEECH_KEY") or os.getenv("AZURE_REGION"):
        return "azure"
    return "mock"


def provider_info(app=None) -> dict[str, str]:
    app = app or current_app
    return {
        "translation_provider": _configured_provider(app),
        "tts_provider": _configured_tts_provider(app),
    }


def configured_tts_voices(app=None, provider: str | None = None) -> dict[str, str]:
    app = app or current_app
    provider = provider or _configured_tts_provider(app)
    voices: dict[str, str] = {}
    for lang in ("pl", "en", "de"):
        key = f"tts_voice_{provider}_{lang}"
        val = AppSetting.get(key) or app.config.get(key.upper())  # allow env/config override
        if val:
            voices[lang] = val
    return voices


def tts_voice_label(tts_backend: TextToSpeechBackend, language: str) -> str | None:
    if hasattr(tts_backend, "voice_label"):
        try:
            return tts_backend.voice_label(language)
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def configured_tts_voice_for_language(app, provider: str, language: str) -> str | None:
    key = f"tts_voice_{provider}_{language}"
    return AppSetting.get(key) or app.config.get(key.upper())


def build_translation_service(app=None) -> TranslationBackend:
    class FallbackTranslationService:
        def __init__(self, primary: TranslationBackend, fallback: TranslationBackend) -> None:
            self.primary = primary
            self.fallback = fallback

        def translate(self, text: str, source_language: str, target_language: str) -> str:
            try:
                return self.primary.translate(text, source_language, target_language)
            except SentenceProcessingError as exc:
                current_app.logger.warning("Primary translator failed (%s), fallback to mock.", exc)
            except Exception as exc:  # pragma: no cover - defensive
                current_app.logger.warning("Unexpected translation error, fallback to mock: %s", exc)
            return self.fallback.translate(text, source_language, target_language)

    app = app or current_app
    provider = _configured_provider(app)
    mock_translator = MockTranslationService()
    if provider == "aws":
        region = app.config.get("AWS_TRANSLATE_REGION") or app.config.get("S3_REGION")
        try:
            aws_translator = AWSTranslateService(region_name=region)
            return FallbackTranslationService(primary=aws_translator, fallback=mock_translator)
        except SentenceProcessingError as exc:
            app.logger.warning("AWS Translate niedostępny, używam tłumacza mock: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            app.logger.warning("Nieoczekiwany błąd AWS Translate, fallback do mock: %s", exc)
    if provider == "openai":
        api_key = app.config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        model = app.config.get("OPENAI_TRANSLATE_MODEL", "gpt-4o-mini")
        try:
            openai_translator = OpenAITranslationService(api_key=api_key, model=model)
            return FallbackTranslationService(primary=openai_translator, fallback=mock_translator)
        except SentenceProcessingError as exc:
            app.logger.warning("OpenAI niedostępny, używam tłumacza mock: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            app.logger.warning("Nieoczekiwany błąd OpenAI, fallback do mock: %s", exc)
    return mock_translator


def build_tts_service(app=None) -> TextToSpeechBackend:
    app = app or current_app
    provider = _configured_tts_provider(app)
    voices = app.config.get("AZURE_SPEECH_VOICES")
    voice_overrides = configured_tts_voices(app, provider)
    mock_tts = MockTextToSpeechService()

    if provider == "azure":
        key = app.config.get("AZURE_SPEECH_KEY") or os.getenv("AZURE_SPEECH_KEY")
        region = app.config.get("AZURE_REGION") or os.getenv("AZURE_REGION")
        if key and region:
            try:
                return AzureTextToSpeechService(key=key, region=region, voices=voices, voice_overrides=voice_overrides)
            except SentenceProcessingError as exc:
                app.logger.warning("Azure TTS niedostępny, używam silnika mock: %s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                app.logger.warning("Nieoczekiwany błąd Azure TTS, fallback do mock: %s", exc)
        else:
            app.logger.warning("Brak klucza lub regionu Azure TTS, fallback do mock.")

    if provider == "google":
        cred_path = app.config.get("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        fallbacks = app.config.get("STT_LANG_FALLBACKS") or os.getenv("STT_LANG_FALLBACKS")
        try:
            return GoogleTextToSpeechService(
                credentials_path=cred_path,
                language_fallbacks=fallbacks,
                voice_overrides=voice_overrides,
            )
        except SentenceProcessingError as exc:
            app.logger.warning("Google TTS niedostępny, używam silnika mock: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            app.logger.warning("Nieoczekiwany błąd Google TTS, fallback do mock: %s", exc)

    return mock_tts


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

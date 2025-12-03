from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import requests
from flask import current_app


class SentenceGenerationError(RuntimeError):
    pass


@dataclass
class GeneratedSentence:
    text: str


@dataclass
class GeneratedResult:
    sentences: list[GeneratedSentence]
    raw_response: str


class MockSentenceGenerator:
    def generate(self, prompt: str) -> GeneratedResult:  # pragma: no cover - simple fallback
        cleaned = (prompt or "").strip()
        base = cleaned[:30] or "zdanie"
        sentences = [
            GeneratedSentence(text=f"Przykładowe zdanie 1 ({base})"),
            GeneratedSentence(text=f"Przykładowe zdanie 2 ({base})"),
        ]
        return GeneratedResult(sentences=sentences, raw_response="fallback: mock generator")


class SentenceGenerationService:
    def __init__(self) -> None:
        self.api_key = current_app.config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.model = current_app.config.get("OPENAI_GENERATOR_MODEL") or os.getenv("OPENAI_GENERATOR_MODEL") or "gpt-4o-mini"
        self.endpoint = current_app.config.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1/chat/completions"
        self.fallback = MockSentenceGenerator()

    def _extract_json_text(self, content: str) -> str:
        trimmed = (content or "").strip()
        # Wyciągnij blok z ```json ... ``` lub ``` ... ```
        fence_match = re.search(r"```(?:json)?\s*(.*?)```", trimmed, re.DOTALL | re.IGNORECASE)
        if fence_match:
            return fence_match.group(1).strip()

        # Szukaj pierwszego znaku { lub [ i ostatniego zamykającego
        start_idx = min((idx for idx in [trimmed.find("{"), trimmed.find("[") if "[" in trimmed else -1] if idx >= 0), default=-1)
        if start_idx >= 0:
            candidate = trimmed[start_idx:]
            last_brace = max(candidate.rfind("}"), candidate.rfind("]"))
            if last_brace >= 0:
                return candidate[: last_brace + 1]
        return trimmed

    def _parse_content(self, content: str) -> list[GeneratedSentence]:
        json_text = self._extract_json_text(content)
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise SentenceGenerationError("Model nie zwrócił poprawnego JSON.") from exc

        # Akceptujemy listę w root lub listę pod typowymi kluczami
        list_payload = None
        if isinstance(data, list):
            list_payload = data
        elif isinstance(data, dict):
            for key in ("sentences", "items", "data", "results", "result", "lista", "list"):  # typowe + PL
                value = data.get(key)
                if isinstance(value, list):
                    list_payload = value
                    break
            # Jeżeli klucze są numeryczne, potraktuj wartości jako listę
            if list_payload is None and data and all(isinstance(k, str) and k.isdigit() for k in data.keys()):
                list_payload = list(data.values())
            # W ostateczności weź pierwszą listę z wartości słownika
            if list_payload is None:
                for value in data.values():
                    if isinstance(value, list):
                        list_payload = value
                        break

        sentences: list[GeneratedSentence] = []
        if isinstance(list_payload, list):
            for item in list_payload:
                if isinstance(item, str):
                    cleaned = item.strip()
                    if cleaned:
                        sentences.append(GeneratedSentence(text=cleaned))
                elif isinstance(item, dict):
                    # Szukaj w polach text/sentence/content
                    for key in ("text", "sentence", "content"):
                        if item.get(key):
                            cleaned = str(item[key]).strip()
                            if cleaned:
                                sentences.append(GeneratedSentence(text=cleaned))
                            break

        if not sentences:
            raise SentenceGenerationError("JSON nie zawiera listy zdań.")
        return sentences

    def generate(self, prompt: str) -> GeneratedResult:
        cleaned = (prompt or "").strip()
        if not cleaned:
            raise SentenceGenerationError("Prompt nie może być pusty.")
        if not self.api_key:
            return self.fallback.generate(cleaned)

        raw_text: str | None = None
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "Zwróć wyłącznie JSON z listą zdań w kluczu root (lista)."},
                {"role": "user", "content": cleaned},
            ],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        }

        try:
            response = requests.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30,
                json=payload,
            )
            raw_text = response.text
        except Exception as exc:  # pragma: no cover - network error path
            fallback = self.fallback.generate(cleaned)
            return GeneratedResult(sentences=fallback.sentences, raw_response=fallback.raw_response)

        if response.status_code >= 400:
            fallback = self.fallback.generate(cleaned)
            return GeneratedResult(sentences=fallback.sentences, raw_response=raw_text or fallback.raw_response)

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            raw_text = content or raw_text
        except Exception as exc:  # pragma: no cover - defensive
            raise SentenceGenerationError("Nie udało się odczytać odpowiedzi modelu.") from exc

        try:
            sentences = self._parse_content(content)
            return GeneratedResult(sentences=sentences, raw_response=raw_text or content or "")
        except SentenceGenerationError:
            fallback = self.fallback.generate(cleaned)
            return GeneratedResult(sentences=fallback.sentences, raw_response=raw_text or fallback.raw_response)

# SentenceTrainer (learning_platform)

Osobna aplikacja Flask służąca do nauki języków przez tłumaczenie przykładowych zdań oraz odsłuchiwanie nagrań audio hostowanych w S3 lub lokalnie.

## Wymagania

- Python 3.11+
- Zależności z `requirements.txt`
- Baza kompatybilna z SQLAlchemy/Alembic (domyślnie SQLite)
- Konto AWS S3 lub lokalny filesystem na potrzeby plików audio

## Konfiguracja środowiska

Aplikacja wczytuje zmienne najpierw z `/home/vs/projects/ai/data_settings/.env`, następnie z lokalnego `./.env`. Kluczowe parametry:

- `DB_URL` – np. `sqlite:///./learning_platform.db` lub `postgresql+psycopg://user:pass@host/db`.
- `FLASK_SECRET_KEY` – sekret sesji Flask-Login.
- `S3_BUCKET`, `S3_REGION`, `S3_BASE_URL`, `S3_LEARNING_PREFIX` – konfiguracja Amazon S3 dla nagrań (prefix domyślny `sentence-trainer`).
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` – standardowe klucze AWS odczytywane przez boto3.

Jeśli `S3_BUCKET` nie jest ustawiony, pliki audio trafiają do `app/static/audio/` (wraz z serwowaniem z `/static/audio/...`).

## Uruchomienie lokalne

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export FLASK_APP=app:create_app
flask run --debug
# lub
python run.py
```

Przy pierwszym starcie tabele (`lp_students`, `lp_sentences`) zostaną utworzone automatycznie. Jeśli potrzebujesz migracji, zintegruj projekt z Alembikiem (`alembic init migrations`, `alembic revision --autogenerate`, `alembic upgrade head`) – modele korzystają z czystego SQLAlchemy, więc konfiguracja przebiega analogicznie jak w projekcie Maildesk.

## Funkcjonalności

- Rejestracja i logowanie uczniów (`/auth/register`, `/auth/login`).
- Lista własnych zdań (`/sentences`) z filtrem po języku źródłowym oraz wyszukiwaniem w treści.
- Formularz dodawania nowego zdania (`/sentences/new`) z natychmiastowym podglądem tłumaczenia i odnośnikami do audio.
- Usuwanie wpisów wraz z plikami audio (przycisk „Usuń” w tabeli i endpoint `DELETE /api/sentences/<id>`).
- API chronione sesją ucznia:
  - `GET /api/sentences`
  - `POST /api/sentences`
  - `GET /api/sentences/<id>`
- Tłumaczenia (Amazon Translate lub tryb mock) + nagrania audio (Azure Cognitive Services Speech lub tryb mock).
- Logika tłumaczeń/audio ma wyraźne punkty rozszerzeń: klasy `MockTranslationService`, `MockTextToSpeechService`, `S3Storage`/`LocalStorage` można zastąpić produkcyjnymi backendami.

### Tłumaczenia

Domyślnie używany jest prosty tłumacz mock (dla developmentu). Aby uzyskać realne tłumaczenia:

```
TRANSLATION_PROVIDER=aws
AWS_TRANSLATE_REGION=eu-west-2  # lub region usługi Amazon Translate
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

Przy aktywnym `TRANSLATION_PROVIDER=aws` aplikacja używa Amazon Translate poprzez boto3, zachowując fallback do trybu mock w razie błędu.

### Synteza mowy (Azure TTS)

Domyślnie nagrania audio są mockiem. Aby korzystać z Azure Cognitive Services Speech:

```
AZURE_SPEECH_KEY=...
AZURE_REGION=westeurope
# opcjonalnie nadpisz głosy:
AZURE_VOICE_PL=pl-PL-ZofiaNeural
AZURE_VOICE_EN=en-US-AriaNeural
AZURE_VOICE_DE=de-DE-KatjaNeural
```

Aplikacja generuje MP3 w formacie `audio-24khz-48kbitrate-mono-mp3` i zapisuje je w S3 z `ACL=public-read`. Jeśli zmienne nie są ustawione, wykorzystywany jest mock TTS. 

## Testy

```bash
pytest
```

Pokrywają one logikę wyboru języków, walidację oraz przypisanie zdań do zalogowanego użytkownika.

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from .translation import SentenceProcessingError


class StorageBackend(Protocol):
    def upload_audio(self, data: bytes, key: str, content_type: str = "audio/mpeg") -> str:  # pragma: no cover
        ...

    def delete_audio(self, key: str) -> None:  # pragma: no cover - interface
        ...


class LocalStorage(StorageBackend):
    def __init__(self, base_dir: Path, public_prefix: str = "/static/audio") -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.public_prefix = public_prefix.rstrip("/")

    def upload_audio(self, data: bytes, key: str, content_type: str = "audio/mpeg") -> str:  # noqa: ARG002 - content type unused
        safe_key = key.lstrip("/")
        target = self.base_dir / safe_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return f"{self.public_prefix}/{safe_key}"

    def delete_audio(self, key: str) -> None:
        safe_key = key.lstrip("/")
        target = self.base_dir / safe_key
        if target.exists():
            target.unlink()


class S3Storage(StorageBackend):
    def __init__(self, bucket: str, region: str | None = None, base_url: str | None = None) -> None:
        if not bucket:
            raise SentenceProcessingError("Brak konfiguracji bucketu S3.")
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - executed only when boto3 missing
            raise SentenceProcessingError("Wymagany jest pakiet boto3 do zapisu audio.") from exc
        session = boto3.session.Session(region_name=region)
        self.client = session.client("s3")
        self.bucket = bucket
        self.region = region
        self.base_url = base_url.rstrip("/") if base_url else None

    def upload_audio(self, data: bytes, key: str, content_type: str = "audio/mpeg") -> str:
        safe_key = key.lstrip("/")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=safe_key,
                Body=data,
                ContentType=content_type,
                ACL="public-read",
            )
        except Exception as exc:  # pragma: no cover - depends on AWS connectivity
            raise SentenceProcessingError("Nie udało się zapisać pliku audio w S3.") from exc

        if self.base_url:
            return f"{self.base_url}/{safe_key}"
        if self.region:
            return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{safe_key}"
        return f"https://{self.bucket}.s3.amazonaws.com/{safe_key}"

    def delete_audio(self, key: str) -> None:
        safe_key = key.lstrip("/")
        try:
            self.client.delete_object(Bucket=self.bucket, Key=safe_key)
        except Exception as exc:  # pragma: no cover - depends on AWS connectivity
            raise SentenceProcessingError("Nie udało się usunąć pliku audio z S3.") from exc


def build_storage(app) -> StorageBackend:
    bucket = app.config.get("S3_BUCKET")
    region = app.config.get("S3_REGION") or os.getenv("S3_REGION")
    base_url = app.config.get("S3_BASE_URL")
    if bucket:
        return S3Storage(bucket=bucket, region=region, base_url=base_url)
    local_dir = Path(app.root_path) / "static" / "audio"
    public_prefix = app.config.get("S3_LEARNING_PREFIX", "/static/audio")
    return LocalStorage(local_dir, public_prefix)

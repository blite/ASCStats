"""Raw landing zone. Report files are stored byte-for-byte as received, before any parsing,
so a load can always be replayed from raw without asking App Store Connect again."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from django.conf import settings


class RawStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete_prefix(self, prefix: str) -> None: ...


class FileStore:
    def __init__(self, root: str):
        self.root = Path(root)

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())

    def delete_prefix(self, prefix: str) -> None:
        for key in self.list(prefix):
            (self.root / key).unlink()


class S3Store:
    """S3 API: MinIO locally, S3 on EKS, GCS through its S3-interoperability endpoint."""

    def __init__(self, bucket: str, prefix: str = "", endpoint_url: str | None = None):
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.s3 = boto3.client("s3", endpoint_url=endpoint_url)

    def ensure_bucket(self) -> None:
        existing = {b["Name"] for b in self.s3.list_buckets().get("Buckets", [])}
        if self.bucket not in existing:
            self.s3.create_bucket(Bucket=self.bucket)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def list(self, prefix: str) -> list[str]:
        keys = []
        strip = len(self.prefix) + 1 if self.prefix else 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            keys += [obj["Key"][strip:] for obj in page.get("Contents", [])]
        return sorted(keys)

    def delete_prefix(self, prefix: str) -> None:
        keys = self.list(prefix)
        for i in range(0, len(keys), 1000):
            self.s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": self._key(k)} for k in keys[i : i + 1000]]},
            )


def raw_store() -> RawStore:
    url = urlparse(settings.RAW_STORAGE_URL)
    if url.scheme == "file":
        return FileStore(url.path)
    if url.scheme == "s3":
        return S3Store(url.netloc, url.path, endpoint_url=settings.S3_ENDPOINT_URL)
    raise ValueError(f"unsupported RAW_STORAGE_URL {settings.RAW_STORAGE_URL!r}")

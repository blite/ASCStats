"""The only place the App Store Connect private key is read.

Two sources, first match wins:

1. ``ASC_KEY_ID`` + ``ASC_ISSUER_ID`` env vars and the .p8 at ``ASC_KEY_PATH``. In Kubernetes
   the key is a Secret mounted as a file (never an env var, so it does not show up in
   ``kubectl describe`` or crash dumps).
2. The shared App Store Connect setup on a developer machine. ``~/.appstoreconnect/config.json``
   holds the team's ``issuer_id`` at the top level and one entry per tool naming that tool's
   key; ASCStats reads ``ascstats.key_id`` and ignores every other entry. The key itself is
   Apple's standard ``~/.appstoreconnect/private_keys/AuthKey_<key id>.p8``. Without an
   ``ascstats`` entry the only .p8 there is used; with several, nothing is guessed.
   ASCStats only reads this file, and nothing is copied into this repo.
"""

import json
import os
from pathlib import Path

from .client import ASCClient, Credentials

DEFAULT_KEY_PATH = "/var/run/secrets/asc/AuthKey.p8"
ASC_CONFIG = "~/.appstoreconnect/config.json"
PRIVATE_KEYS_DIR = "~/.appstoreconnect/private_keys"
CONFIG_ENTRY = "ascstats"


class MissingCredentials(RuntimeError):
    pass


def _from_env() -> Credentials | None:
    key_path = Path(os.environ.get("ASC_KEY_PATH", DEFAULT_KEY_PATH))
    key_id = os.environ.get("ASC_KEY_ID", "")
    issuer_id = os.environ.get("ASC_ISSUER_ID", "")
    if key_id and issuer_id and key_path.is_file():
        return Credentials(key_id=key_id, issuer_id=issuer_id, private_key=key_path.read_text())
    return None


def local_key() -> tuple[str, str, Path] | None:
    """(key id, issuer id, .p8 path) from the shared developer-machine setup, or None when
    there is no config file. A config that exists but does not resolve to exactly one key
    raises, so a misconfigured machine is never silently skipped."""
    config_path = Path(ASC_CONFIG).expanduser()
    if not config_path.is_file():
        return None
    config = json.loads(config_path.read_text())
    issuer_id = config.get("issuer_id")
    if not issuer_id:
        raise MissingCredentials(f"{ASC_CONFIG} has no top-level issuer_id")
    keys_dir = Path(PRIVATE_KEYS_DIR).expanduser()
    key_id = (config.get(CONFIG_ENTRY) or {}).get("key_id")
    if key_id:
        key_path = keys_dir / f"AuthKey_{key_id}.p8"
        if not key_path.is_file():
            raise MissingCredentials(f"{ASC_CONFIG} names key {key_id}, but {key_path} is missing")
        return key_id, issuer_id, key_path
    found = sorted(keys_dir.glob("AuthKey_*.p8"))
    if len(found) != 1:
        names = ", ".join(p.name for p in found) or "none"
        raise MissingCredentials(
            f'{ASC_CONFIG} has no "{CONFIG_ENTRY}": {{"key_id": ...}} entry and '
            f"{PRIVATE_KEYS_DIR} holds {len(found)} keys ({names}); add the entry to pick one"
        )
    return found[0].stem.removeprefix("AuthKey_"), issuer_id, found[0]


def _from_developer_machine() -> Credentials | None:
    if (found := local_key()) is None:
        return None
    key_id, issuer_id, key_path = found
    return Credentials(key_id=key_id, issuer_id=issuer_id, private_key=key_path.read_text())


def load_credentials() -> Credentials:
    credentials = _from_env() or _from_developer_machine()
    if credentials is None:
        raise MissingCredentials(
            "set ASC_KEY_ID, ASC_ISSUER_ID and mount the .p8 key at ASC_KEY_PATH, or set up "
            f'{ASC_CONFIG} (top-level issuer_id, "{CONFIG_ENTRY}": {{"key_id": ...}}) with '
            f"the key at {PRIVATE_KEYS_DIR}/AuthKey_<key id>.p8"
        )
    return credentials


def client_from_env() -> ASCClient:
    return ASCClient(load_credentials())


if __name__ == "__main__":
    # For k8s/scripts/asc-secret-from-local.sh: print where the local key is, never its
    # contents.
    if (found := local_key()) is None:
        raise SystemExit(f"no {ASC_CONFIG}")
    print(*found, sep="\n")

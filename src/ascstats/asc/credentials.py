"""The only place the App Store Connect private key is read.

In Kubernetes the .p8 key is a Secret mounted as a file (never an env var, so it does not
show up in ``kubectl describe`` or crash dumps). ``ASC_KEY_PATH`` points at that file.
"""

import os
from pathlib import Path

from .client import ASCClient, Credentials

DEFAULT_KEY_PATH = "/var/run/secrets/asc/AuthKey.p8"


class MissingCredentials(RuntimeError):
    pass


def load_credentials() -> Credentials:
    key_path = Path(os.environ.get("ASC_KEY_PATH", DEFAULT_KEY_PATH))
    key_id = os.environ.get("ASC_KEY_ID", "")
    issuer_id = os.environ.get("ASC_ISSUER_ID", "")
    if not (key_id and issuer_id and key_path.is_file()):
        raise MissingCredentials(
            f"set ASC_KEY_ID, ASC_ISSUER_ID and mount the .p8 key at {key_path}"
        )
    return Credentials(key_id=key_id, issuer_id=issuer_id, private_key=key_path.read_text())


def client_from_env() -> ASCClient:
    return ASCClient(load_credentials())

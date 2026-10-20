import json

import pytest

from ascstats.asc import credentials


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("ASC_KEY_ID", "ASC_ISSUER_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ASC_KEY_PATH", str(tmp_path / "missing.p8"))
    (tmp_path / ".appstoreconnect/private_keys").mkdir(parents=True)
    return tmp_path


def write_config(home, **config):
    (home / ".appstoreconnect/config.json").write_text(json.dumps(config))


def install_key(home, key_id):
    (home / f".appstoreconnect/private_keys/AuthKey_{key_id}.p8").write_text(f"PEM {key_id}")


def test_env_and_mounted_file_win(home, monkeypatch):
    write_config(home, issuer_id="team", ascstats={"key_id": "ASCKEY"})
    install_key(home, "ASCKEY")
    (home / "mounted.p8").write_text("MOUNTED")
    monkeypatch.setenv("ASC_KEY_PATH", str(home / "mounted.p8"))
    monkeypatch.setenv("ASC_KEY_ID", "ENVKEY")
    monkeypatch.setenv("ASC_ISSUER_ID", "env-issuer")
    creds = credentials.load_credentials()
    assert (creds.key_id, creds.private_key) == ("ENVKEY", "MOUNTED")


def test_reads_its_own_entry_and_ignores_other_tools(home):
    write_config(
        home, issuer_id="team", other_tool={"key_id": "OTHERKEY"}, ascstats={"key_id": "ASCKEY"}
    )
    install_key(home, "OTHERKEY")
    install_key(home, "ASCKEY")
    creds = credentials.load_credentials()
    assert (creds.key_id, creds.issuer_id, creds.private_key) == ("ASCKEY", "team", "PEM ASCKEY")


def test_without_an_entry_uses_the_only_key(home):
    write_config(home, issuer_id="team", other_tool={"key_id": "OTHER"})
    install_key(home, "ONLYKEY")
    assert credentials.load_credentials().key_id == "ONLYKEY"


def test_without_an_entry_several_keys_are_never_guessed(home):
    write_config(home, issuer_id="team")
    install_key(home, "AAA")
    install_key(home, "BBB")
    with pytest.raises(credentials.MissingCredentials, match=r"AuthKey_AAA\.p8, AuthKey_BBB\.p8"):
        credentials.load_credentials()


def test_named_key_must_exist(home):
    write_config(home, issuer_id="team", ascstats={"key_id": "GONE"})
    install_key(home, "OTHER")
    with pytest.raises(credentials.MissingCredentials, match=r"AuthKey_GONE\.p8 is missing"):
        credentials.load_credentials()


def test_issuer_id_is_required(home):
    write_config(home, ascstats={"key_id": "ASCKEY"})
    install_key(home, "ASCKEY")
    with pytest.raises(credentials.MissingCredentials, match="issuer_id"):
        credentials.load_credentials()

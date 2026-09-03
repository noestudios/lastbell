"""secrets.py: the keyring probe and the error text that points at the env
backend (install Phase 3a)."""
from __future__ import annotations

import pytest

from lastbell import secrets as secretstore


def _backend(priority):
    return type("Stub", (), {"priority": priority})()


def test_keyring_available_true_for_a_real_backend(monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: _backend(5))
    assert secretstore.keyring_available()


def test_keyring_available_false_for_fail_and_null_backends(monkeypatch):
    import keyring
    from keyring.backends import fail, null
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    assert not secretstore.keyring_available()
    monkeypatch.setattr(keyring, "get_keyring", lambda: null.Keyring())
    assert not secretstore.keyring_available()


def test_keyring_available_false_when_loading_the_backend_raises(monkeypatch):
    import keyring
    from keyring.errors import NoKeyringError

    def boom():
        raise NoKeyringError("No recommended backend was available")
    monkeypatch.setattr(keyring, "get_keyring", boom)
    assert not secretstore.keyring_available()


def test_get_password_keyring_errors_mention_env_backend(monkeypatch):
    import keyring
    from keyring.errors import NoKeyringError

    def boom(service, username):
        raise NoKeyringError("no backend")
    monkeypatch.setattr(keyring, "get_password", boom)
    with pytest.raises(secretstore.SecretError, match="LASTBELL_SECRET_BACKEND=env"):
        secretstore.get_password("parent1")

    monkeypatch.setattr(keyring, "get_password", lambda s, u: None)
    with pytest.raises(secretstore.SecretError, match="env"):
        secretstore.get_password("parent1")


def test_get_password_env_backend(monkeypatch):
    monkeypatch.delenv("LASTBELL_PASSWORD", raising=False)
    with pytest.raises(secretstore.SecretError, match="lastbell setup"):
        secretstore.get_password("parent1", "env")
    monkeypatch.setenv("LASTBELL_PASSWORD", "hunter2")
    assert secretstore.get_password("parent1", "env") == "hunter2"


def test_set_password_wraps_keyring_failure(monkeypatch):
    import keyring
    from keyring.errors import NoKeyringError

    def boom(service, username, password):
        raise NoKeyringError("no backend")
    monkeypatch.setattr(keyring, "set_password", boom)
    with pytest.raises(secretstore.SecretError, match="lastbell setup"):
        secretstore.set_password("parent1", "pw")


def test_smtp_password_env_var_wins_and_no_keyring_means_empty(monkeypatch):
    import keyring
    monkeypatch.setenv("LASTBELL_PASSWORD_SMTP", "from-env")
    assert secretstore.get_smtp_password() == "from-env"
    monkeypatch.delenv("LASTBELL_PASSWORD_SMTP")

    def boom(service, username):
        raise RuntimeError("no backend")
    monkeypatch.setattr(keyring, "get_password", boom)
    assert secretstore.get_smtp_password() == ""


def test_env_backend_never_touches_the_keyring(monkeypatch):
    """On the env backend the optional lookups return '' without importing
    keyring at all — a headless box's keyring can block forever."""
    import sys

    from lastbell import secrets

    monkeypatch.setenv("LASTBELL_SECRET_BACKEND", "env")
    monkeypatch.delenv("LASTBELL_CANVAS_TOKEN", raising=False)
    monkeypatch.delenv("LASTBELL_PASSWORD_SMTP", raising=False)

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("keyring must not be touched on the env backend")

    monkeypatch.setitem(sys.modules, "keyring", Boom())
    assert secrets.get_canvas_token() == ""
    assert secrets.get_smtp_password() == ""
    monkeypatch.setenv("LASTBELL_CANVAS_TOKEN", "t0k")
    assert secrets.get_canvas_token() == "t0k"


def test_env_backend_stores_the_canvas_token_in_the_settings_file(monkeypatch, tmp_path):
    from lastbell import paths, secrets

    monkeypatch.setenv("LASTBELL_SECRET_BACKEND", "env")
    env_file = tmp_path / ".env"
    env_file.write_text("LASTBELL_DISTRICT=x\n")
    monkeypatch.setattr(paths, "active_env_file", lambda: env_file)
    where = secrets.set_canvas_token("abc")
    assert str(env_file) in where
    assert "LASTBELL_CANVAS_TOKEN=abc" in env_file.read_text()

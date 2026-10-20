"""验证分层配置的来源优先级、边界和敏感信息保护。"""

import logging
import os
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from pydantic import ValidationError

from smtp_gateway import GatewayConfig
from smtp_gateway.config import ConfigurationFileError


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("SMTP_GATEWAY_"):
            monkeypatch.delenv(name)


def _values() -> dict[str, object]:
    return {
        "http": {"auth": {"username": "manager", "password": "test-api-credential"}},
        "smtp": {
            "auth": {"password": "test-submission-credential"},
            "allow_insecure_auth": True,
        },
        "database": {"encryption_key": Fernet.generate_key().decode()},
    }


def _set(values: dict[str, object], path: tuple[str, ...], value: object) -> None:
    target = values
    for part in path[:-1]:
        child = target[part]
        assert isinstance(child, dict)
        target = child
    target[path[-1]] = value


def _write_config(tmp_path: Path, values: dict[str, object]) -> Path:
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path


def _set_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "HTTP__AUTH__USERNAME": "manager",
        "HTTP__AUTH__PASSWORD": "environment-api-credential",
        "SMTP__AUTH__PASSWORD": "environment-submission-credential",
        "SMTP__ALLOW_INSECURE_AUTH": "true",
        "DATABASE__ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }.items():
        monkeypatch.setenv(f"SMTP_GATEWAY_{name}", value)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("http", "auth", "password"), ""),
        (("http", "auth", "username"), ""),
        (("database", "encryption_key"), "invalid"),
        (("smtp", "auth", "password"), None),
        (("smtp", "auth", "password"), ""),
        (("smtp", "auth", "username"), ""),
        (("smtp", "allow_insecure_auth"), False),
        (("database", "url"), "mysql+asyncmy://localhost/database"),
        (("database", "url"), "invalid-url"),
        (("delivery",), {"lease_seconds": 60, "timeout": 60}),
        (("smtp", "tls"), {"cert_file": "certificate.pem"}),
    ],
)
def test_invalid_runtime_configuration_is_rejected(
    path: tuple[str, ...], value: object
) -> None:
    values = _values()
    _set(values, path, value)
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(values)


@pytest.mark.parametrize(
    "path",
    [
        ("unknown",),
        ("http", "unknown"),
        ("http", "auth", "unknown"),
        ("smtp", "unknown"),
        ("smtp", "auth", "unknown"),
        ("smtp", "tls"),
        ("database", "unknown"),
        ("limits",),
        ("delivery",),
    ],
)
def test_unknown_fields_are_rejected_at_every_level(path: tuple[str, ...]) -> None:
    values = _values()
    _set(values, path, {"unknown": True})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GatewayConfig.model_validate(values)


def test_validated_configuration_does_not_expose_secrets() -> None:
    values = _values()
    database_url = "postgresql+asyncpg://username:private-db-credential@db/gateway"
    _set(values, ("database", "url"), database_url)
    config = GatewayConfig.model_validate(values)
    for value in [
        config.http.auth.password,
        config.smtp.auth.password,
        config.database.encryption_key,
        config.database.url,
    ]:
        assert value.get_secret_value() not in repr(config)
        assert value.get_secret_value() not in config.model_dump_json()


def test_configuration_is_frozen_at_every_level() -> None:
    config = GatewayConfig.model_validate(_values())
    for section in [
        config,
        config.http,
        config.http.auth,
        config.smtp,
        config.smtp.auth,
        config.smtp.tls,
        config.database,
        config.limits,
        config.delivery,
    ]:
        field = next(iter(type(section).model_fields))
        with pytest.raises(ValidationError, match="Instance is frozen"):
            setattr(section, field, getattr(section, field))


def test_yaml_load_keeps_defaults(tmp_path: Path) -> None:
    config = GatewayConfig.load(_write_config(tmp_path, _values()))
    assert config.http.host == "127.0.0.1"
    assert config.http.port == 18081
    assert config.smtp.host == "127.0.0.1"
    assert config.smtp.port == 1025
    assert config.smtp.auth.username == "grafana"
    assert (
        config.database.url.get_secret_value() == "sqlite+aiosqlite:///smtp-gateway.db"
    )
    assert config.limits.max_message_bytes == 10 * 1024 * 1024
    assert config.limits.max_recipients == 100
    assert config.delivery.timeout == 60
    assert config.delivery.lease_seconds == 120
    assert config.delivery.concurrency == 4


def test_environment_overrides_yaml_recursively_without_erasing_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _values()
    values["http"] = {
        "host": "127.0.0.2",
        "port": 18090,
        "auth": {"username": "file-manager", "password": "file-api-credential"},
    }
    values["delivery"] = {"timeout": 20, "lease_seconds": 40}
    monkeypatch.setenv("SMTP_GATEWAY_HTTP__PORT", "18100")
    monkeypatch.setenv(
        "SMTP_GATEWAY_HTTP__AUTH__PASSWORD", "environment-api-credential"
    )
    monkeypatch.setenv("SMTP_GATEWAY_DELIVERY", '{"max_attempts":7,"retry_delay":2}')
    monkeypatch.setenv("SMTP_GATEWAY_DELIVERY__RETRY_DELAY", "3")
    config = GatewayConfig.load(_write_config(tmp_path, values))
    assert config.http.port == 18100
    assert config.http.host == "127.0.0.2"
    assert config.http.auth.username == "file-manager"
    assert config.http.auth.password.get_secret_value() == "environment-api-credential"
    assert config.delivery.max_attempts == 7
    assert config.delivery.retry_delay == 3
    assert config.delivery.timeout == 20
    assert config.delivery.lease_seconds == 40


def test_environment_can_supply_all_required_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_environment(monkeypatch)
    config = GatewayConfig.load()
    assert config.http.auth.username == "manager"
    assert config.smtp.auth.username == "grafana"


def test_loading_without_path_does_not_search_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("invalid: [", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _set_required_environment(monkeypatch)
    assert GatewayConfig.load().http.auth.username == "manager"


@pytest.mark.parametrize("document", ["[]", "false", "42", "plaintext", "123: value"])
def test_configuration_file_requires_a_mapping_with_string_keys(
    tmp_path: Path, document: str
) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigurationFileError, match="mapping with string keys"):
        GatewayConfig.load(path)


def test_missing_configuration_file_does_not_fall_back_to_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_required_environment(monkeypatch)
    with pytest.raises(ConfigurationFileError, match="Unable to read"):
        GatewayConfig.load(tmp_path / "missing.yaml")


def test_invalid_yaml_reports_a_safe_error(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("password: [private-malformed-credential", encoding="utf-8")
    with pytest.raises(ConfigurationFileError, match="invalid YAML") as error:
        GatewayConfig.load(path)
    assert "private-malformed-credential" not in str(error.value)


def test_yaml_paths_remain_relative_to_the_working_directory(tmp_path: Path) -> None:
    values = _values()
    _set(
        values, ("smtp", "tls"), {"cert_file": "certificate.pem", "key_file": "key.pem"}
    )
    config = GatewayConfig.load(_write_config(tmp_path, values))
    assert config.smtp.tls.cert_file == Path("certificate.pem")
    assert config.smtp.tls.key_file == Path("key.pem")
    assert (
        config.database.url.get_secret_value() == "sqlite+aiosqlite:///smtp-gateway.db"
    )


def test_raw_settings_debug_logs_do_not_expose_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("PYDANTIC_SETTINGS_DEBUG", "1")
    monkeypatch.setenv("SMTP_GATEWAY_HTTP__AUTH__PASSWORD", "private-debug-credential")
    caplog.set_level(logging.DEBUG, logger="pydantic_settings")
    config = GatewayConfig.load(_write_config(tmp_path, _values()))
    assert config.http.auth.password.get_secret_value() == "private-debug-credential"
    assert "private-debug-credential" not in caplog.text
    assert logging.getLogger("pydantic_settings").getEffectiveLevel() >= logging.WARNING

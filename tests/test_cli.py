"""CLI 使用分层配置启动，并保持配置失败信息不含原文。"""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from cryptography.fernet import Fernet

import smtp_gateway.cli as gateway_cli


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    for name in tuple(os.environ):
        if name.startswith("SMTP_GATEWAY_"):
            monkeypatch.delenv(name)
    create_app = Mock(return_value=object())
    run = Mock()
    monkeypatch.setattr(gateway_cli, "create_app", create_app)
    monkeypatch.setattr(gateway_cli.uvicorn, "run", run)
    monkeypatch.setattr(gateway_cli, "version", Mock(return_value="0.1.0"))
    return create_app, run


def _values() -> dict[str, object]:
    return {
        "http": {
            "host": "127.0.0.2",
            "port": 18100,
            "auth": {"username": "manager", "password": "private-api-credential"},
        },
        "smtp": {
            "auth": {"password": "private-submission-credential"},
            "allow_insecure_auth": True,
        },
        "database": {"encryption_key": Fernet.generate_key().decode()},
    }


def test_cli_loads_yaml_and_uses_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: tuple[Mock, Mock]
) -> None:
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(yaml.safe_dump(_values()), encoding="utf-8")
    monkeypatch.setenv("SMTP_GATEWAY_HTTP__PORT", "18101")
    monkeypatch.setattr(
        sys, "argv", ["smtp-gateway", "--config-file", str(config_path)]
    )
    gateway_cli.main()
    create_app, run = runtime
    config = create_app.call_args.args[0]
    assert config.http.auth.username == "manager"
    assert config.http.port == 18101
    run.assert_called_once_with(create_app.return_value, host="127.0.0.2", port=18101)


def test_cli_without_config_file_uses_only_environment(
    monkeypatch: pytest.MonkeyPatch, runtime: tuple[Mock, Mock]
) -> None:
    for name, value in {
        "HTTP__AUTH__USERNAME": "manager",
        "HTTP__AUTH__PASSWORD": "private-api-credential",
        "SMTP__AUTH__PASSWORD": "private-submission-credential",
        "SMTP__ALLOW_INSECURE_AUTH": "true",
        "DATABASE__ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }.items():
        monkeypatch.setenv(f"SMTP_GATEWAY_{name}", value)
    monkeypatch.setattr(sys, "argv", ["smtp-gateway"])
    gateway_cli.main()
    create_app, run = runtime
    run.assert_called_once_with(create_app.return_value, host="127.0.0.1", port=18081)


@pytest.mark.parametrize(
    "document",
    [
        "password: [private-malformed-credential",
        "- private-malformed-credential",
        "false",
    ],
)
def test_cli_reports_invalid_configuration_file_without_source_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[Mock, Mock],
    document: str,
) -> None:
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(document, encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["smtp-gateway", "--config-file", str(config_path)]
    )
    with pytest.raises(
        SystemExit, match="Invalid SMTP gateway configuration file"
    ) as error:
        gateway_cli.main()
    assert "private-malformed-credential" not in str(error.value)
    create_app, run = runtime
    create_app.assert_not_called()
    run.assert_not_called()


def test_cli_reports_missing_configuration_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: tuple[Mock, Mock]
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["smtp-gateway", "--config-file", str(tmp_path / "missing.yaml")]
    )
    with pytest.raises(SystemExit, match="Unable to read configuration file"):
        gateway_cli.main()
    runtime[1].assert_not_called()


def test_cli_validation_error_does_not_echo_sensitive_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: tuple[Mock, Mock]
) -> None:
    values = _values()
    values["database"] = {
        "url": "postgresql+asyncpg://user:private-db-credential@localhost/database",
        "encryption_key": "private-invalid-encryption-key",
    }
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(yaml.safe_dump(values), encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["smtp-gateway", "--config-file", str(config_path)]
    )
    with pytest.raises(SystemExit, match="database.encryption_key") as error:
        gateway_cli.main()
    assert "private-db-credential" not in str(error.value)
    assert "private-invalid-encryption-key" not in str(error.value)
    assert "private-api-credential" not in str(error.value)
    runtime[1].assert_not_called()


def test_cli_environment_json_error_does_not_echo_input(
    monkeypatch: pytest.MonkeyPatch, runtime: tuple[Mock, Mock]
) -> None:
    monkeypatch.setenv("SMTP_GATEWAY_HTTP", "private-invalid-json-credential")
    monkeypatch.setattr(sys, "argv", ["smtp-gateway"])
    with pytest.raises(SystemExit, match="environment configuration") as error:
        gateway_cli.main()
    assert "private-invalid-json-credential" not in str(error.value)
    runtime[1].assert_not_called()

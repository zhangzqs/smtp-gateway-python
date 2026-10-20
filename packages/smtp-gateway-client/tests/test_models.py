"""共享模型校验 SMTP 接口边界并隐藏敏感字段。"""

import pytest
from pydantic import SecretStr, ValidationError

from smtp_gateway_client import SmtpSettings, SmtpSettingsWrite, TlsMode


@pytest.mark.parametrize("mode", ["none", "starttls", "implicit"])
def test_tls_modes_have_the_same_wire_values_in_both_models(mode: str) -> None:
    request = SmtpSettingsWrite.model_validate(
        {"host": "smtp.example.com", "port": 587, "tls": mode}
    )
    response = SmtpSettings.model_validate({"configured": True, "tls": mode})
    assert request.tls == TlsMode(mode)
    assert response.tls == TlsMode(mode)
    assert request.model_dump(mode="json")["tls"] == mode
    assert response.model_dump(mode="json")["tls"] == mode


@pytest.mark.parametrize("port", [0, 65536])
@pytest.mark.parametrize("model", [SmtpSettings, SmtpSettingsWrite])
def test_port_range_is_checked_for_requests_and_responses(
    port: int, model: type[SmtpSettings] | type[SmtpSettingsWrite]
) -> None:
    values: dict[str, object] = {"host": "smtp.example.com", "port": port}
    if model is SmtpSettings:
        values["configured"] = True
    with pytest.raises(ValidationError, match="port"):
        model.model_validate(values)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "smtp.example.com/path",
        "sender@smtp.example.com",
        "smtp.example.com:587",
        " smtp.example.com",
        "smtp.example.com ",
        "smtp.\texample.com",
        "smtp.example.com\r\n",
    ],
)
def test_host_rejects_embedded_credentials_paths_and_whitespace(host: str) -> None:
    with pytest.raises(ValidationError, match="host"):
        SmtpSettingsWrite(host=host, port=587)


def test_unknown_request_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SmtpSettingsWrite.model_validate(
            {"host": "smtp.example.com", "port": 587, "read_only": True}
        )


def test_password_is_hidden_by_model_serialization_and_repr() -> None:
    value = "private-upstream-credential"
    body = SmtpSettingsWrite(
        host="smtp.example.com", port=587, password=SecretStr(value)
    )
    assert value not in repr(body)
    assert value not in body.model_dump_json()
    assert body.password is not None
    assert body.password.get_secret_value() == value


def test_response_filters_secret_fields_and_preserves_read_only() -> None:
    response = SmtpSettings.model_validate(
        {"configured": False, "password": "private-response-credential"}
    )
    assert response.read_only is False
    assert response.port == 587
    assert response.host == ""
    assert "password" not in response.model_dump()
    assert "private-response-credential" not in repr(response)
    assert response.model_copy(update={"read_only": True}).read_only is True

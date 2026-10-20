"""共享模型校验 SMTP 接口边界并隐藏敏感字段。"""

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError

from smtp_gateway_client import (
    DeliveryState,
    MessageContent,
    MessagePage,
    MessageRecord,
    SmtpSettings,
    SmtpSettingsWrite,
    TlsMode,
)


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
            {"host": "smtp.example.com", "port": 587, "unexpected_setting": True}
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


def test_response_filters_secret_fields_and_keeps_setting_defaults() -> None:
    response = SmtpSettings.model_validate(
        {"configured": False, "password": "private-response-credential"}
    )
    assert response.port == 587
    assert response.host == ""
    assert "password" not in response.model_dump()
    assert "private-response-credential" not in repr(response)


def test_message_record_normalizes_time_and_filters_nonpublic_fields() -> None:
    response = MessageRecord.model_validate(
        {
            "id": "message-id",
            "sender": "sender@example.com",
            "created_at": "2026-10-20T12:30:00+08:00",
            "content": "private-email-body",
            "password": "private-response-credential",
            "deliveries": [
                {
                    "recipient": "recipient@example.com",
                    "state": "delivered",
                    "attempts": 2,
                    "last_error": None,
                    "claim_token": "private-claim-token",
                }
            ],
        }
    )
    assert response.created_at == datetime(2026, 10, 20, 4, 30, tzinfo=UTC)
    assert response.subject == ""
    assert response.model_dump(mode="json")["created_at"] == "2026-10-20T04:30:00Z"
    assert response.deliveries[0].state == DeliveryState.DELIVERED
    assert response.deliveries[0].attempts == 2
    assert "private-email-body" not in response.model_dump_json()
    assert "private-response-credential" not in response.model_dump_json()
    assert "private-claim-token" not in response.model_dump_json()


@pytest.mark.parametrize("timestamp", ["2026-10-20T12:30:00", "not-a-time"])
def test_message_record_rejects_timestamps_without_a_valid_time_zone(
    timestamp: str,
) -> None:
    with pytest.raises(ValidationError, match="created_at"):
        MessageRecord.model_validate(
            {
                "id": "message-id",
                "sender": "sender@example.com",
                "created_at": timestamp,
                "deliveries": [],
            }
        )


def test_message_page_defaults_to_twenty_items_on_first_page() -> None:
    response = MessagePage(items=[], total=0)
    assert response.page == 1
    assert response.page_size == 20


@pytest.mark.parametrize(
    "values",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
        {"total": -1},
    ],
)
def test_message_page_rejects_invalid_pagination(values: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        MessagePage.model_validate({"items": [], "total": 0, **values})


def test_message_content_keeps_optional_bodies_and_filters_unrelated_fields() -> None:
    response = MessageContent.model_validate(
        {
            "id": "message-id",
            "subject": "告警测试",
            "text": None,
            "html": "<p>Alert details</p>",
            "password": "private-response-credential",
            "attachments": ["private-attachment"],
        }
    )
    assert response.text is None
    assert response.html == "<p>Alert details</p>"
    assert response.subject == "告警测试"
    assert set(response.model_dump()) == {"id", "subject", "text", "html"}
    assert "private-response-credential" not in response.model_dump_json()
    assert "private-attachment" not in response.model_dump_json()
    assert "Alert details" not in repr(response)

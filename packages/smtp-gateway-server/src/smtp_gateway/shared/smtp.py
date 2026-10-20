"""校验 SMTP 信封地址，不改写原始邮件。"""

from email.errors import HeaderParseError
from email.headerregistry import Address


def validate_address(value: str) -> str:
    """使用标准邮件解析器验证不含展示名称的 ASCII 信封地址。"""
    if not value or len(value) > 320 or any(char in value for char in "\r\n\x00"):
        raise ValueError("A valid mailbox address is required")
    try:
        value.encode("ascii")
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError) as exc:
        raise ValueError("An ASCII mailbox address is required") from exc
    if not address.username or not address.domain or address.addr_spec != value:
        raise ValueError("A mailbox without a display name is required")
    return value


__all__ = ["validate_address"]

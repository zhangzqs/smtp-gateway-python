"""独立于 GPUStack 业务数据库的网关表。"""

from sqlalchemy import Float, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from smtp_gateway.shared import DeliveryState, TlsMode


class GatewayBase(DeclarativeBase):
    """网关数据库的独立 metadata。"""


class SmtpSettingsRow(GatewayBase):
    __tablename__ = "smtp_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(default=0)
    host: Mapped[str | None] = mapped_column(String(253))
    port: Mapped[int] = mapped_column(default=587)
    username: Mapped[str] = mapped_column(String(320), default="")
    secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    tls: Mapped[str] = mapped_column(String(16), default=TlsMode.STARTTLS)


class MessageRow(GatewayBase):
    __tablename__ = "smtp_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sender: Mapped[str] = mapped_column(String(320))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[float] = mapped_column(Float)


class DeliveryRow(GatewayBase):
    __tablename__ = "smtp_deliveries"
    __table_args__ = (Index("ix_smtp_due", "state", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("smtp_messages.id", ondelete="CASCADE"), index=True
    )
    recipient: Mapped[str] = mapped_column(String(320))
    state: Mapped[str] = mapped_column(String(16), default=DeliveryState.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float)
    lease_until: Mapped[float | None] = mapped_column(Float)
    claim_token: Mapped[str | None] = mapped_column(String(36))
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[float | None] = mapped_column(Float)


__all__ = ["DeliveryRow", "GatewayBase", "MessageRow", "SmtpSettingsRow"]

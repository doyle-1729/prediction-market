import secrets
from datetime import datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import (
    Integer, String, Numeric, DateTime, ForeignKey, Enum, Boolean, Text, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from app.database import Base


class MarketStatus(str, enum.Enum):
    open = "open"
    closed = "closed"
    resolved = "resolved"


class OrderSide(str, enum.Enum):
    buy = "buy"
    sell = "sell"


class OrderOutcome(str, enum.Enum):
    yes = "yes"
    no = "no"


class OrderStatus(str, enum.Enum):
    open = "open"
    partial = "partial"
    filled = "filled"
    cancelled = "cancelled"


class OrderType(str, enum.Enum):
    limit = "limit"
    market = "market"


class GroupRole(str, enum.Enum):
    member = "member"
    admin = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1000.00"))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    orders: Mapped[list["Order"]] = relationship("Order", back_populates="user")
    positions: Mapped[list["Position"]] = relationship("Position", back_populates="user")
    group_memberships: Mapped[list["GroupMembership"]] = relationship("GroupMembership", back_populates="user")


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    invite_code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(8))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    created_by: Mapped["User"] = relationship("User")
    memberships: Mapped[list["GroupMembership"]] = relationship("GroupMembership", back_populates="group")
    markets: Mapped[list["Market"]] = relationship("Market", back_populates="group")


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[GroupRole] = mapped_column(Enum(GroupRole), default=GroupRole.member)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    group: Mapped["Group"] = relationship("Group", back_populates="memberships")
    user: Mapped["User"] = relationship("User", back_populates="group_memberships")


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[MarketStatus] = mapped_column(
        Enum(MarketStatus), default=MarketStatus.open
    )
    resolved_outcome: Mapped[str | None] = mapped_column(
        Enum(OrderOutcome), nullable=True
    )
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    created_by: Mapped["User"] = relationship("User")
    group: Mapped[Optional["Group"]] = relationship("Group", back_populates="markets")
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="market")
    positions: Mapped[list["Position"]] = relationship("Position", back_populates="market")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    outcome: Mapped[OrderOutcome] = mapped_column(Enum(OrderOutcome), nullable=False)
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide), nullable=False)
    # Price in cents (0–100), representing probability %
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType), default=OrderType.limit)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.open
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship("User", back_populates="orders")
    market: Mapped["Market"] = relationship("Market", back_populates="orders")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    buy_order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    sell_order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    outcome: Mapped[OrderOutcome] = mapped_column(Enum(OrderOutcome), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    market: Mapped["Market"] = relationship("Market")
    buy_order: Mapped["Order"] = relationship("Order", foreign_keys=[buy_order_id])
    sell_order: Mapped["Order"] = relationship("Order", foreign_keys=[sell_order_id])


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    yes_shares: Mapped[int] = mapped_column(Integer, default=0)
    no_shares: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped["User"] = relationship("User", back_populates="positions")
    market: Mapped["Market"] = relationship("Market", back_populates="positions")

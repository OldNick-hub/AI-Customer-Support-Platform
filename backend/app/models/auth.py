"""Identity, User & Role-Based Access Control (RBAC) Models.

Defines internal enterprise users, roles, and many-to-many permission mappings.
"""

import uuid
from datetime import datetime
from typing import List, Optional
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSONBType, StringArrayType, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class UserRole(Base):
    """Many-to-many association table between Users and Roles."""

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    user: Mapped["User"] = relationship("User", back_populates="user_roles")
    role: Mapped["Role"] = relationship("Role", back_populates="role_users")


class Role(Base, UUIDPrimaryKeyMixin):
    """Enterprise permission roles (ADMIN, SUPERVISOR, AGENT, AUDITOR)."""

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    permissions: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)

    role_users: Mapped[List["UserRole"]] = relationship(
        "UserRole", back_populates="role", cascade="all, delete-orphan", lazy="selectin"
    )
    users: Mapped[List["User"]] = relationship(
        "User", secondary="user_roles", back_populates="roles", viewonly=True, lazy="selectin"
    )


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Enterprise support agents, supervisors, and administrators."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(100), nullable=False)
    skills: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)
    max_active_tickets: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_on_call: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    user_roles: Mapped[List["UserRole"]] = relationship(
        "UserRole", back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    roles: Mapped[List["Role"]] = relationship(
        "Role", secondary="user_roles", back_populates="users", viewonly=True, lazy="selectin"
    )
    assigned_tickets: Mapped[List["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="assigned_agent"
    )

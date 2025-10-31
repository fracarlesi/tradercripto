"""User repository for async database operations."""

import hashlib
import secrets
from datetime import UTC, datetime

from database.models import User, UserAuthSession
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session


class UserRepository:
    """Repository for User CRUD operations."""

    @staticmethod
    def _hash_password(password: str) -> str:
        """Hash password using SHA-256.

        Args:
            password: Plain text password

        Returns:
            Hexadecimal hash string
        """
        return hashlib.sha256(password.encode()).hexdigest()

    @staticmethod
    async def create_user(
        db: AsyncSession,
        username: str,
        email: str | None = None,
        password: str | None = None,
    ) -> User:
        """Create a new user.

        Args:
            db: Async database session
            username: Username
            email: Email address (optional)
            password: Plain text password (optional, will be hashed)

        Returns:
            Created User instance
        """
        user = User(
            username=username,
            email=email,
            password_hash=(UserRepository._hash_password(password) if password else None),
            is_active=True,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)
        return user

    @staticmethod
    async def get_or_create_user(db: AsyncSession, username: str = "default") -> User:
        """Get existing user or create default user.

        Args:
            db: Async database session
            username: Username (default: "default")

        Returns:
            User instance
        """
        # Try to get existing user
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()

        if user:
            return user

        # Create default user
        return await UserRepository.create_user(db, username=username)

    @staticmethod
    async def get_by_username(db: AsyncSession, username: str) -> User | None:
        """Get user by username.

        Args:
            db: Async database session
            username: Username to search

        Returns:
            User instance or None if not found
        """
        result = await db.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id(db: AsyncSession, user_id: int) -> User | None:
        """Get user by ID.

        Args:
            db: Async database session
            user_id: User ID

        Returns:
            User instance or None if not found
        """
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def verify_password(db: AsyncSession, username: str, password: str) -> User | None:
        """Verify user credentials.

        Args:
            db: Async database session
            username: Username
            password: Plain text password to verify

        Returns:
            User instance if credentials valid, None otherwise
        """
        user = await UserRepository.get_by_username(db, username)
        if not user or not user.password_hash:
            return None

        password_hash = UserRepository._hash_password(password)
        if password_hash == user.password_hash:
            return user

        return None

    @staticmethod
    async def create_auth_session(
        db: AsyncSession, user_id: int, ip_address: str | None = None
    ) -> UserAuthSession:
        """Create authentication session for user.

        Args:
            db: Async database session
            user_id: User ID
            ip_address: Client IP address (optional)

        Returns:
            Created UserAuthSession instance
        """
        session_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC).replace(tzinfo=None)  # Add 24 hours in production

        auth_session = UserAuthSession(
            user_id=user_id,
            session_token=session_token,
            ip_address=ip_address,
            expires_at=expires_at,
        )
        db.add(auth_session)
        await db.flush()
        return auth_session

    @staticmethod
    async def get_auth_session(db: AsyncSession, session_token: str) -> UserAuthSession | None:
        """Get authentication session by token.

        Args:
            db: Async database session
            session_token: Session token

        Returns:
            UserAuthSession instance or None if not found/expired
        """
        result = await db.execute(
            select(UserAuthSession).where(UserAuthSession.session_token == session_token)
        )
        session = result.scalar_one_or_none()

        if not session:
            return None

        # Check if expired
        if session.expires_at and session.expires_at < datetime.now(UTC):
            return None

        return session


# Sync helper functions for legacy routes using sync Session
def get_user(db: Session, user_id: int) -> User | None:
    """Get user by ID (sync version).

    Args:
        db: Sync database session
        user_id: User ID

    Returns:
        User instance or None if not found
    """
    return db.query(User).filter(User.id == user_id).first()


def get_or_create_user(db: Session, username: str = "default") -> User:
    """Get or create user by username (sync version).

    Args:
        db: Sync database session
        username: Username (default: "default")

    Returns:
        User instance
    """
    # Try to get existing user
    user = db.query(User).filter(User.username == username).first()

    if user:
        return user

    # Create new user
    user = User(
        username=username,
        email=f"{username}@example.com",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def verify_auth_session(db: Session, session_token: str) -> int | None:
    """Verify auth session and return user_id (sync version).

    Args:
        db: Sync database session
        session_token: Session token to verify

    Returns:
        User ID if valid, None otherwise
    """
    session = (
        db.query(UserAuthSession)
        .filter(UserAuthSession.session_token == session_token)
        .first()
    )

    if not session:
        return None

    # Check if expired
    if session.expires_at and session.expires_at < datetime.now(UTC).replace(tzinfo=None):
        return None

    return session.user_id


def user_has_password(db: Session, user_id: int) -> bool:
    """Check if user has a password set (sync version).

    Args:
        db: Sync database session
        user_id: User ID

    Returns:
        True if user has password, False otherwise
    """
    user = db.query(User).filter(User.id == user_id).first()
    return bool(user and user.password_hash)


def set_user_password(db: Session, user_id: int, password: str) -> User | None:
    """Set user password (sync version).

    Args:
        db: Sync database session
        user_id: User ID
        password: Plain text password

    Returns:
        Updated User instance or None if not found
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return None

    user.password_hash = UserRepository._hash_password(password)
    db.commit()
    db.refresh(user)
    return user


def verify_user_password(db: Session, user_id: int, password: str) -> bool:
    """Verify user password (sync version).

    Args:
        db: Sync database session
        user_id: User ID
        password: Plain text password to verify

    Returns:
        True if password matches, False otherwise
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.password_hash:
        return False

    password_hash = UserRepository._hash_password(password)
    return password_hash == user.password_hash

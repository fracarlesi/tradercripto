from pydantic import BaseModel


class UserCreate(BaseModel):
    """Create a new user for authentication"""

    username: str
    email: str | None = None
    password: str | None = None  # For future authentication if needed


class UserUpdate(BaseModel):
    """Update user information"""

    username: str | None = None
    email: str | None = None


class UserOut(BaseModel):
    """User output for authentication"""

    id: int
    username: str
    email: str | None = None
    is_active: bool

    class Config:
        from_attributes = True


class UserLogin(BaseModel):
    """User login credentials"""

    username: str
    password: str


class UserAuthResponse(BaseModel):
    """User authentication response"""

    user: UserOut
    session_token: str
    expires_at: str

from pydantic import BaseModel


class AccountCreate(BaseModel):
    """Create a new AI Trading Account"""

    name: str  # Display name (e.g., "GPT Trader", "Claude Analyst")
    model: str = "gpt-4-turbo"
    base_url: str = "https://api.openai.com/v1"
    api_key: str
    initial_capital: float = 10000.0
    account_type: str = "AI"  # "AI" or "MANUAL"


class AccountUpdate(BaseModel):
    """Update AI Trading Account"""

    name: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class AccountOut(BaseModel):
    """AI Trading Account output"""

    id: int
    user_id: int
    name: str
    model: str
    base_url: str
    api_key: str  # Will be masked in API responses
    initial_capital: float
    current_cash: float
    frozen_cash: float
    account_type: str
    is_active: bool

    class Config:
        from_attributes = True


class AccountOverview(BaseModel):
    """Account overview with portfolio information"""

    account: AccountOut
    total_assets: float  # Total assets in USD
    positions_value: float  # Total positions value in USD

from typing import Literal

from pydantic import BaseModel, field_validator

OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]


class OrderCreate(BaseModel):
    user_id: int
    symbol: str
    name: str
    market: Literal["US", "HK"]
    side: OrderSide
    order_type: OrderType
    price: float | None = None
    quantity: int
    username: str | None = None  # Username for verification (required if no session_token)
    password: str | None = None  # Trading password (required if no session_token)
    session_token: str | None = None  # Auth session token (alternative to username+password)

    @field_validator("quantity")
    @classmethod
    def quantity_positive(cls, v):
        if v <= 0:
            raise ValueError("quantity must be positive")
        return v

    @field_validator("price")
    @classmethod
    def price_non_negative(cls, v, info):
        return v


class OrderOut(BaseModel):
    id: int
    order_no: str
    user_id: int
    symbol: str
    name: str
    market: str
    side: str
    order_type: str
    price: float | None
    quantity: int
    filled_quantity: int
    status: str

    class Config:
        from_attributes = True

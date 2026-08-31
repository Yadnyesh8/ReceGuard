"""Pydantic request/response models.

`raceguard init` reads the request models off each route to generate
`raceguard.yaml` payloads, so keep every field's type honest and every
field required unless the endpoint genuinely treats it as optional.
"""

from __future__ import annotations

from pydantic import BaseModel


class CreateOrderRequest(BaseModel):
    item_id: int
    qty: int


class OrderResponse(BaseModel):
    item_id: int
    qty: int
    remaining_stock: int


class TransferRequest(BaseModel):
    from_id: int
    to_id: int
    amount: int


class TransferResponse(BaseModel):
    from_id: int
    to_id: int
    amount: int
    from_balance: int
    to_balance: int


class RedeemCouponRequest(BaseModel):
    code: str
    redeemed_by: str


class RedeemCouponResponse(BaseModel):
    code: str
    redeemed_by: str
    redemption_id: int


class SignupRequest(BaseModel):
    email: str


class SignupResponse(BaseModel):
    id: int
    email: str


class AddMemberRequest(BaseModel):
    user_email: str


class AddMemberResponse(BaseModel):
    team_id: int
    user_email: str
    member_count: int


class PurchaseRequest(BaseModel):
    qty: int


class PurchaseResponse(BaseModel):
    product_id: int
    qty: int
    remaining_stock: int

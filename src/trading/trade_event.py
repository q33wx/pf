"""Parse pump.fun TradeEvent data from transaction logs."""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from typing import Any

import base58

# TradeEvent discriminator (sha256("event:TradeEvent")[:8])
TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])
EVENT_DISCRIMINATOR_SIZE = 8
LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6


@dataclass
class TradeEvent:
    """Decoded pump.fun TradeEvent."""

    mint: str
    sol_amount_lamports: int
    token_amount_raw: int
    is_buy: bool
    user: str
    timestamp: int
    virtual_sol_reserves: int
    virtual_token_reserves: int

    @property
    def sol_amount(self) -> float:
        """SOL amount as decimal."""
        return self.sol_amount_lamports / LAMPORTS_PER_SOL

    @property
    def token_amount(self) -> float:
        """Token amount as decimal."""
        return self.token_amount_raw / (10**TOKEN_DECIMALS)

    @property
    def price_per_token(self) -> float:
        """Implied SOL price per token from this trade."""
        if self.token_amount_raw <= 0:
            return 0.0
        return (self.sol_amount_lamports * (10**TOKEN_DECIMALS)) / (
            self.token_amount_raw * LAMPORTS_PER_SOL
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for logging."""
        return {
            "mint": self.mint,
            "sol_amount": self.sol_amount,
            "token_amount": self.token_amount,
            "is_buy": self.is_buy,
            "user": self.user,
            "timestamp": self.timestamp,
            "price_per_token": self.price_per_token,
        }


def parse_trade_events_from_logs(logs: list[str]) -> list[TradeEvent]:
    """Extract all TradeEvents from a list of log lines.

    Args:
        logs: Transaction log messages.

    Returns:
        List of decoded TradeEvent objects (may be empty).
    """
    events: list[TradeEvent] = []
    for log in logs:
        if "Program data:" not in log:
            continue
        try:
            encoded = log.split("Program data: ", 1)[1].strip()
            decoded = base64.b64decode(encoded)
        except (ValueError, binascii.Error, IndexError):
            continue

        if len(decoded) < EVENT_DISCRIMINATOR_SIZE:
            continue
        if decoded[:EVENT_DISCRIMINATOR_SIZE] != TRADE_EVENT_DISCRIMINATOR:
            continue

        event = decode_trade_event(decoded[EVENT_DISCRIMINATOR_SIZE:])
        if event is not None:
            events.append(event)
    return events


def decode_trade_event(data: bytes) -> TradeEvent | None:
    """Decode TradeEvent core fields from raw bytes.

    Supports progressive IDL versions by only requiring the core fields
    (mint, sol_amount, token_amount, is_buy, user, timestamp, virtual reserves).

    Args:
        data: Event payload after the 8-byte discriminator.

    Returns:
        TradeEvent or None if data is too short / invalid.
    """
    # Core: 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8 = 105
    if len(data) < 105:
        return None

    offset = 0
    mint = base58.b58encode(data[offset : offset + 32]).decode()
    offset += 32

    sol_amount = struct.unpack_from("<Q", data, offset)[0]
    offset += 8

    token_amount = struct.unpack_from("<Q", data, offset)[0]
    offset += 8

    is_buy = bool(data[offset])
    offset += 1

    user = base58.b58encode(data[offset : offset + 32]).decode()
    offset += 32

    timestamp = struct.unpack_from("<q", data, offset)[0]
    offset += 8

    virtual_sol_reserves = struct.unpack_from("<Q", data, offset)[0]
    offset += 8

    virtual_token_reserves = struct.unpack_from("<Q", data, offset)[0]

    return TradeEvent(
        mint=mint,
        sol_amount_lamports=sol_amount,
        token_amount_raw=token_amount,
        is_buy=is_buy,
        user=user,
        timestamp=timestamp,
        virtual_sol_reserves=virtual_sol_reserves,
        virtual_token_reserves=virtual_token_reserves,
    )

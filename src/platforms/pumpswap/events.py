"""Parse PumpSwap BuyEvent / SellEvent from transaction logs for volume tracking."""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass

import base58

# From idl/pump_swap_idl.json
BUY_EVENT_DISCRIMINATOR = bytes([103, 244, 82, 31, 44, 245, 119, 119])
SELL_EVENT_DISCRIMINATOR = bytes([62, 47, 55, 10, 165, 3, 220, 42])
EVENT_DISCRIMINATOR_SIZE = 8
LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6

# Offsets after discriminator for both buy/sell core numeric prefix (all 8-byte fields)
# Buy:  timestamp, base_out, max_quote_in, user_base, user_quote, pool_base, pool_quote, quote_in
# Sell: timestamp, base_in,  min_quote_out, user_base, user_quote, pool_base, pool_quote, quote_out
_QUOTE_AMOUNT_OFFSET = 56  # 7 * 8
_BASE_AMOUNT_OFFSET = 8
_POOL_OFFSET = 112  # 14 * 8  (after 14 u64/i64 fields)


@dataclass
class PumpSwapSwap:
    """A buy or sell on a PumpSwap pool."""

    is_buy: bool
    sol_amount_lamports: int
    token_amount_raw: int
    pool: str
    user: str

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
        """Implied SOL per token."""
        if self.token_amount_raw <= 0:
            return 0.0
        return (self.sol_amount_lamports * (10**TOKEN_DECIMALS)) / (
            self.token_amount_raw * LAMPORTS_PER_SOL
        )


def parse_pumpswap_swap_from_logs(
    logs: list[str], *, token_is_base: bool = True
) -> list[PumpSwapSwap]:
    """Extract BuyEvent/SellEvent swaps from program logs.

    Args:
        logs: Transaction log lines.
        token_is_base: Pool orientation. For inverted pools (base = WSOL)
            a program BuyEvent means someone bought SOL — i.e. SOLD the
            token — so side and amount fields are flipped accordingly.

    Returns:
        List of decoded swaps (may be empty), normalized so ``is_buy`` and
        ``sol_amount``/``token_amount`` always refer to the traded TOKEN.
    """
    swaps: list[PumpSwapSwap] = []
    for log in logs:
        if "Program data:" not in log:
            continue
        try:
            encoded = log.split("Program data: ", 1)[1].strip()
            decoded = base64.b64decode(encoded)
        except (ValueError, binascii.Error, IndexError):
            continue

        if len(decoded) < EVENT_DISCRIMINATOR_SIZE + _POOL_OFFSET + 64:
            continue

        disc = decoded[:EVENT_DISCRIMINATOR_SIZE]
        payload = decoded[EVENT_DISCRIMINATOR_SIZE:]

        if disc == BUY_EVENT_DISCRIMINATOR:
            swap = _decode_swap(payload, base_bought=True, token_is_base=token_is_base)
        elif disc == SELL_EVENT_DISCRIMINATOR:
            swap = _decode_swap(payload, base_bought=False, token_is_base=token_is_base)
        else:
            continue

        if swap is not None:
            swaps.append(swap)
    return swaps


def _decode_swap(
    data: bytes, *, base_bought: bool, token_is_base: bool
) -> PumpSwapSwap | None:
    """Decode shared numeric prefix + pool/user pubkeys from Buy/Sell event."""
    if len(data) < _POOL_OFFSET + 64:
        return None

    base_amount_raw = struct.unpack_from("<Q", data, _BASE_AMOUNT_OFFSET)[0]
    quote_amount_raw = struct.unpack_from("<Q", data, _QUOTE_AMOUNT_OFFSET)[0]
    pool = base58.b58encode(data[_POOL_OFFSET : _POOL_OFFSET + 32]).decode()
    user = base58.b58encode(data[_POOL_OFFSET + 32 : _POOL_OFFSET + 64]).decode()

    if token_is_base:
        token_amount_raw = base_amount_raw
        sol_amount_lamports = quote_amount_raw
        is_buy = base_bought
    else:
        token_amount_raw = quote_amount_raw
        sol_amount_lamports = base_amount_raw
        is_buy = not base_bought  # buying base SOL == selling the token

    return PumpSwapSwap(
        is_buy=is_buy,
        sol_amount_lamports=sol_amount_lamports,
        token_amount_raw=token_amount_raw,
        pool=pool,
        user=user,
    )

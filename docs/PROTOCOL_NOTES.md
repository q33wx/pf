# Protocol notes

Hard-won pump.fun / PumpSwap protocol details that the code alone won't
teach. Read this before touching instruction building or account layouts.

## Pump.fun protocol notes (gotchas)

- The vendored IDL at `idl/pump_fun_idl.json` is **incomplete**: it does not list `bonding-curve-v2` (BC trades) or `pool-v2` (PumpSwap trades). Both are required on-chain. `bonding-curve-v2` PDA seed is `["bonding-curve-v2", mint]` under the pump program; `pool-v2` is `["pool-v2", base_mint]` under the pump-amm program. Always cross-check account lists against a recent successful on-chain tx — IDL alone will produce broken code.
- BC `buy` ix is **18 accounts** (post 2026-04-28 upgrade). The trailing account is one of 8 `BREAKING_FEE_RECIPIENTS` (mutable), AFTER `bonding-curve-v2`.
- BC `sell` ix is **16 accounts non-cashback / 17 cashback**. Cashback path inserts `user_volume_accumulator` (PDA seed `["user_volume_accumulator", user]`) BEFORE `bonding-curve-v2`. Detect cashback from byte 82 of the bonding-curve account data, or via the `is_cashback_coin` field returned by the curve manager.
- The BondingCurve account is **83 bytes** (was 81): trailing fields are `is_mayhem_mode: bool` then `is_cashback_coin: bool`. PumpSwap Pool is **245 bytes** with the same `is_cashback_coin` byte at offset 244.
- The IDL instruction is `create_v2` (snake_case). `create_v2` args: `name (str), symbol (str), uri (str), creator (pubkey), is_mayhem_mode (bool), is_cashback_enabled (OptionBool 1B)`. `OptionBool` is a struct wrapping a single bool — serialized as 1 byte, not 2.
- `extreme_fast_mode` skips the curve-state RPC fetch — make sure event parsers populate `is_mayhem_mode` and `is_cashback_coin` on `TokenInfo` from the `CreateEvent` payload (otherwise mayhem coins use the wrong fee_recipient and cashback coins use the wrong sell account count).

## Conventions that differ from defaults

- Private keys live only in `.env` (`SOLANA_PRIVATE_KEY`); never hard-code or log them.
- Always cross-check on-chain account lists against a recent successful tx before trusting the IDL (see gotchas above).

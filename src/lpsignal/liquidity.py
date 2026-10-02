"""Add and remove concentrated liquidity from your own wallet (``pip install "lpsignal[liquidity]"``).

The transactions go to the pools' official position managers — Uniswap v3, PancakeSwap v3, Aerodrome / Velodrome
Slipstream — with the position always minted to, and collected by, your own address: no LPSignal contract, no fee.
You read the chain and sign with your own web3.py instance; your keys never reach this SDK.

The math is the pools' own, in integers (TickMath, LiquidityAmounts, SqrtPriceMath rounding, the Uniswap SDK's
slippage rule) — a port of the code the lpsignal.app web app and the Node SDK run, checked against its test vectors.

    from web3 import Web3
    from web3.middleware import SignAndSendRawMiddlewareBuilder
    w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
    acct = w3.eth.account.from_key(KEY)
    w3.middleware_onion.inject(SignAndSendRawMiddlewareBuilder.build(acct), layer=0)
    pool = LPSignal().pool("base", "0x...")["pool"]
    plan = plan_add_liquidity(w3, pool, owner=acct.address, amount0=10**18, range_bp=500)
    send_plan(w3, plan["approvals"] + [plan["mint"]], sender=acct.address)

Never re-send a mint or a partial removal whose receipt you have not seen: a second one would also go through
(send_plan raises TxPending with the hash instead of guessing).
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

# ---- chains and position managers (the ones whose factory created the pools LPSignal tracks) ----
NPM: dict[str, dict[str, str]] = {
    "ethereum": {"uniswap_v3": "0xc36442b4a4522e871399cd717abdd847ab11fe88", "pancake_v3": "0x46a15b0b27311cedf172ab29e4f4766fbe7f4364"},
    "bsc": {"uniswap_v3": "0x7b8a01b39d58278b5de7e48c8449c9f4f5170613", "pancake_v3": "0x46a15b0b27311cedf172ab29e4f4766fbe7f4364"},
    "base": {"uniswap_v3": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1", "pancake_v3": "0x46a15b0b27311cedf172ab29e4f4766fbe7f4364", "aerodrome_cl": "0x827922686190790b37229fd06084350e74485b72"},
    "arbitrum": {"uniswap_v3": "0xc36442b4a4522e871399cd717abdd847ab11fe88", "pancake_v3": "0x46a15b0b27311cedf172ab29e4f4766fbe7f4364"},
    "optimism": {"uniswap_v3": "0xc36442b4a4522e871399cd717abdd847ab11fe88", "velodrome_cl": "0x416b433906b1b72fa758e166e239c43d68dc6f29"},
    "polygon": {"uniswap_v3": "0xc36442b4a4522e871399cd717abdd847ab11fe88"},
}
CHAIN_ID = {"ethereum": 1, "bsc": 56, "base": 8453, "arbitrum": 42161, "optimism": 10, "polygon": 137}
WRAPPED = {
    "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "bsc": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    "base": "0x4200000000000000000000000000000000000006", "arbitrum": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    "optimism": "0x4200000000000000000000000000000000000006", "polygon": "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
}
# USDT on Ethereum refuses to change a non-zero allowance to another non-zero one: reset it to 0 first
ZERO_FIRST = {"ethereum": ["0xdac17f958d2ee523a2206206994597c13d831ec7"]}
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
DEFAULT_SLIPPAGE_BPS = 50
DEFAULT_DEADLINE_S = 20 * 60
_PAGE = 200  # NFTs read per Multicall3 page


def is_slipstream(dex: str) -> bool:
    return dex in ("aerodrome_cl", "velodrome_cl")


def npm_of(chain: str, dex: str) -> Optional[str]:
    return NPM.get(chain, {}).get(dex)


# ---- TickMath (Uniswap v3 core, exact) ----
MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
Q96 = 1 << 96
_MAX_U256 = (1 << 256) - 1
_RATIOS = [
    (0x2, 0xFFF97272373D413259A46990580E213A), (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC), (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644), (0x20, 0xFF973B41FA98C081472E6896DFB254C0), (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053), (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4), (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3), (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9), (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5), (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7), (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9), (0x20000, 0x5D6AF8DEDB81196699C329225EE604), (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
]


def sqrt_ratio_at_tick(tick: int) -> int:
    if not isinstance(tick, int) or tick < MIN_TICK or tick > MAX_TICK:
        raise ValueError(f"tick {tick}")
    a = abs(tick)
    ratio = 0xFFFCB933BD6FAD37AA2D162D1A594001 if a & 1 else 0x100000000000000000000000000000000
    for bit, mul in _RATIOS:
        if a & bit:
            ratio = (ratio * mul) >> 128
    if tick > 0:
        ratio = _MAX_U256 // ratio
    return (ratio >> 32) + (0 if ratio % (1 << 32) == 0 else 1)


# ---- ranges ----
_LN = math.log(1.0001)


def full_range(tick_spacing: int) -> tuple[int, int]:
    s = max(1, tick_spacing)
    return math.ceil(MIN_TICK / s) * s, math.floor(MAX_TICK / s) * s


def range_ticks(tick: int, range_bp: int, tick_spacing: int) -> tuple[int, int]:
    """±range_bp of price around `tick`, widened outward to the tick grid, always containing the price (0 = full range)."""
    s = max(1, tick_spacing)
    min_t, max_t = full_range(s)
    if range_bp == 0:
        return min_t, max_t
    r = range_bp / 1e4
    lo = math.floor((tick - math.log(1 + r) / _LN) / s) * s
    hi = math.ceil((tick + math.log(1 + r) / _LN) / s) * s
    if lo > tick:
        lo = math.floor(tick / s) * s
    if hi <= tick:
        hi = (math.floor(tick / s) + 1) * s
    return max(lo, min_t), min(hi, max_t)


# ---- LiquidityAmounts / SqrtPriceMath ----
def _mul_div_up(a: int, b: int, d: int) -> int:
    p = a * b
    return p // d + (0 if p % d == 0 else 1)


def _sort2(a: int, b: int) -> tuple[int, int]:
    return (b, a) if a > b else (a, b)


def _liq0(sa: int, sb: int, amount0: int) -> int:
    sa, sb = _sort2(sa, sb)
    return amount0 * (sa * sb // Q96) // (sb - sa)


def _liq1(sa: int, sb: int, amount1: int) -> int:
    sa, sb = _sort2(sa, sb)
    return amount1 * Q96 // (sb - sa)


def liquidity_for_amounts(sp: int, sa: int, sb: int, amount0: int, amount1: int) -> int:
    sa, sb = _sort2(sa, sb)
    if sp <= sa:
        return _liq0(sa, sb, amount0)
    if sp < sb:
        return min(_liq0(sp, sb, amount0), _liq1(sa, sp, amount1))
    return _liq1(sa, sb, amount1)


def _amount0(sa: int, sb: int, l: int, up: bool) -> int:
    sa, sb = _sort2(sa, sb)
    n1, n2 = l << 96, sb - sa
    return _mul_div_up(_mul_div_up(n1, n2, sb), 1, sa) if up else (n1 * n2 // sb) // sa


def _amount1(sa: int, sb: int, l: int, up: bool) -> int:
    sa, sb = _sort2(sa, sb)
    return _mul_div_up(l, sb - sa, Q96) if up else l * (sb - sa) // Q96


def amounts_for_liquidity(sp: int, sa: int, sb: int, l: int, up: bool) -> tuple[int, int]:
    sa, sb = _sort2(sa, sb)
    if sp <= sa:
        return _amount0(sa, sb, l, up), 0
    if sp < sb:
        return _amount0(sp, sb, l, up), _amount1(sa, sp, l, up)
    return 0, _amount1(sa, sb, l, up)


def other_amount(side: int, amount: int, sp: int, tick_lower: int, tick_upper: int) -> Optional[int]:
    """Given one side's amount, the other side the range needs at the current price (0: the range takes only one token
    at this price; None: the given side is not used at all there)."""
    sa, sb = sqrt_ratio_at_tick(tick_lower), sqrt_ratio_at_tick(tick_upper)
    if side == 0 and sp >= sb:
        return None
    if side == 1 and sp <= sa:
        return None
    if sp <= sa or sp >= sb:
        return 0
    l = _liq0(sp, sb, amount) if side == 0 else _liq1(sa, sp, amount)
    return amounts_for_liquidity(sp, sa, sb, l, True)[1 if side == 0 else 0]


_BPS = 10_000


def _moved_sqrt(sp: int, bps: int) -> int:
    s = sp * math.isqrt((_BPS + bps) * 10**36 // _BPS) // 10**18
    return MIN_SQRT_RATIO if s < MIN_SQRT_RATIO else MAX_SQRT_RATIO - 1 if s >= MAX_SQRT_RATIO else s


def mint_amounts(sp: int, tick_lower: int, tick_upper: int, desired0: int, desired1: int, slippage_bps: int) -> dict[str, int]:
    """The amounts to send for these desired amounts, and the minimums under a price move of up to `slippage_bps`: what
    the position manager would actually take at the worse end of the band (it re-derives the liquidity from the desired
    amounts at the price it meets) — token0 at the upper price, token1 at the lower."""
    sa, sb = sqrt_ratio_at_tick(tick_lower), sqrt_ratio_at_tick(tick_upper)
    liquidity = liquidity_for_amounts(sp, sa, sb, desired0, desired1)
    s = round(slippage_bps)
    if s < 0 or s >= _BPS:
        raise ValueError("slippage")
    use0, use1 = amounts_for_liquidity(sp, sa, sb, liquidity, True)
    a0, a1 = min(use0, desired0), min(use1, desired1)
    l = liquidity_for_amounts(sp, sa, sb, a0, a1)
    up, down = _moved_sqrt(sp, s), _moved_sqrt(sp, -s)
    min0, _ = amounts_for_liquidity(up, sa, sb, liquidity_for_amounts(up, sa, sb, a0, a1), False)
    _, min1 = amounts_for_liquidity(down, sa, sb, liquidity_for_amounts(down, sa, sb, a0, a1), False)
    return {"liquidity": l, "amount0Desired": a0, "amount1Desired": a1, "amount0Min": min(min0, a0), "amount1Min": min(min1, a1)}


def remove_amounts(sp: int, pos: dict[str, Any], share_bps: int, slippage_bps: int) -> dict[str, int]:
    """Taking `share_bps` (1..10000) of the position out: its liquidity, worth now, and the minimums under the price move."""
    if not isinstance(share_bps, int) or share_bps < 1 or share_bps > 10_000:
        raise ValueError("share")
    s = round(slippage_bps)
    if s < 0 or s >= _BPS:
        raise ValueError("slippage")
    liquidity = pos["liquidity"] if share_bps == 10_000 else pos["liquidity"] * share_bps // _BPS
    sa, sb = sqrt_ratio_at_tick(pos["tickLower"]), sqrt_ratio_at_tick(pos["tickUpper"])
    a0, a1 = amounts_for_liquidity(sp, sa, sb, liquidity, False)
    min0, _ = amounts_for_liquidity(_moved_sqrt(sp, s), sa, sb, liquidity, False)
    _, min1 = amounts_for_liquidity(_moved_sqrt(sp, -s), sa, sb, liquidity, False)
    return {"liquidity": liquidity, "amount0": a0, "amount1": a1, "amount0Min": min(min0, a0), "amount1Min": min(min1, a1)}


# ---- calls ----
def _selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _call(sig: str, types: list[str], args: list[Any]) -> str:
    return "0x" + (_selector(sig) + encode(types, args)).hex()


_MINT_V3 = "(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"
_MINT_SLIP = "(address,address,int24,int24,int24,uint256,uint256,uint256,uint256,address,uint256,uint160)"
_MAX_U128 = (1 << 128) - 1


def _cs(a: str) -> str:
    return to_checksum_address(a)


def mint_call(chain: str, dex: str, token0: str, token1: str, fee: int, tick_spacing: int, tick_lower: int, tick_upper: int,
              amounts: dict[str, int], recipient: str, deadline: int, native_side: Optional[int]) -> dict[str, Any]:
    """The mint transaction (with the native coin: multicall(mint, refundETH) carrying the value)."""
    npm = npm_of(chain, dex)
    if not npm:
        raise ValueError(f"no position manager for {dex} on {chain}")
    if not tick_lower < tick_upper or tick_lower % tick_spacing != 0 or tick_upper % tick_spacing != 0:
        raise ValueError("range not on the tick grid")
    if native_side is not None and (token0 if native_side == 0 else token1).lower() != WRAPPED.get(chain, "").lower():
        raise ValueError("not the wrapped native token")
    a = amounts
    common = [a["amount0Desired"], a["amount1Desired"], a["amount0Min"], a["amount1Min"], _cs(recipient), deadline]
    if is_slipstream(dex):
        mint = _call(f"mint({_MINT_SLIP})", [_MINT_SLIP], [(_cs(token0), _cs(token1), tick_spacing, tick_lower, tick_upper, *common, 0)])
    else:
        mint = _call(f"mint({_MINT_V3})", [_MINT_V3], [(_cs(token0), _cs(token1), fee, tick_lower, tick_upper, *common)])
    if native_side is None:
        return {"to": npm, "data": mint, "value": 0}
    value = a["amount0Desired"] if native_side == 0 else a["amount1Desired"]
    refund = "0x" + _selector("refundETH()").hex()
    data = _call("multicall(bytes[])", ["bytes[]"], [[bytes.fromhex(mint[2:]), bytes.fromhex(refund[2:])]])
    return {"to": npm, "data": data, "value": value}


def approve_call(token: str, spender: str, amount: int) -> dict[str, Any]:
    return {"to": token, "data": _call("approve(address,uint256)", ["address", "uint256"], [_cs(spender), amount]), "value": 0}


def approvals_needed(chain: str, npm: str, needs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The approvals a mint needs given the current allowances (exact amounts, never unlimited)."""
    out = []
    for n in needs:
        if n["amount"] == 0 or n["allowance"] >= n["amount"]:
            continue
        if n["allowance"] > 0 and n["token"].lower() in ZERO_FIRST.get(chain, []):
            out.append(approve_call(n["token"], npm, 0))
        out.append(approve_call(n["token"], npm, n["amount"]))
    return out


def collect_call(chain: str, dex: str, token_id: int, recipient: str) -> dict[str, Any]:
    npm = npm_of(chain, dex)
    if not npm:
        raise ValueError(f"no position manager for {dex} on {chain}")
    return {"to": npm, "data": _call("collect((uint256,address,uint128,uint128))", ["(uint256,address,uint128,uint128)"], [(token_id, _cs(recipient), _MAX_U128, _MAX_U128)]), "value": 0}


def remove_call(chain: str, dex: str, token_id: int, amounts: dict[str, int], recipient: str, deadline: int) -> dict[str, Any]:
    """One transaction: decrease the liquidity (minimums, deadline), then collect everything to the wallet; 0 = fees only."""
    collect = collect_call(chain, dex, token_id, recipient)
    if amounts["liquidity"] == 0:
        return collect
    decrease = _call("decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))", ["(uint256,uint128,uint256,uint256,uint256)"],
                     [(token_id, amounts["liquidity"], amounts["amount0Min"], amounts["amount1Min"], deadline)])
    data = _call("multicall(bytes[])", ["bytes[]"], [[bytes.fromhex(decrease[2:]), bytes.fromhex(collect["data"][2:])]])
    return {"to": collect["to"], "data": data, "value": 0}


# ---- reading the chain and sending (web3.py) ----
class TxPending(Exception):
    """Sent, but the receipt was not seen in time: it may still land — do not send it again blindly."""

    def __init__(self, tx_hash: str):
        super().__init__(f"transaction {tx_hash} sent, receipt not seen yet: check it before sending again")
        self.tx_hash = tx_hash


class TxUnknown(Exception):
    """The send failed without a hash (e.g. the node took it but the answer was lost): it may have been broadcast.
    Check whether `nonce` of `sender` was used before sending anything again."""

    def __init__(self, sender: str, nonce: int):
        super().__init__(f"sending from {sender} failed with an unknown outcome (nonce {nonce}): check whether that nonce was used before sending again")
        self.sender, self.nonce = sender, nonce


class TxReverted(Exception):
    def __init__(self, tx_hash: str):
        super().__init__(f"transaction {tx_hash} reverted")
        self.tx_hash = tx_hash


class StaleRead(Exception):
    """The RPC is behind a block the plan must see (e.g. your previous removal from the same position)."""

    def __init__(self, head: int, min_block: int):
        super().__init__(f"the RPC is at block {head}, behind {min_block}: try again shortly")
        self.head, self.min_block = head, min_block


def _eth_call(w3: Any, to: str, data: str, block: Any = "latest", sender: Optional[str] = None) -> bytes:
    tx: dict[str, Any] = {"to": _cs(to), "data": data}
    if sender:
        tx["from"] = _cs(sender)
    return bytes(w3.eth.call(tx, block_identifier=block))


def _assert_chain(w3: Any, chain: str) -> None:
    cid = w3.eth.chain_id
    if cid != CHAIN_ID.get(chain):
        raise ValueError(f"the web3 client is on chain {cid}, the pool on {chain} ({CHAIN_ID.get(chain)})")


def _slot0(w3: Any, pool: str, block: Any = "latest") -> tuple[int, int]:
    # the first two fields of slot0, the same on Uniswap v3, PancakeSwap v3 and Slipstream
    raw = _eth_call(w3, pool, "0x" + _selector("slot0()").hex(), block)
    sqrt_price, tick = decode(["uint160", "int24"], raw[:64])
    return sqrt_price, tick


def _allowance(w3: Any, token: str, owner: str, spender: str) -> int:
    return decode(["uint256"], _eth_call(w3, token, _call("allowance(address,address)", ["address", "address"], [_cs(owner), _cs(spender)])))[0]


def _bind(call: dict[str, Any], chain: str, sender: str) -> dict[str, Any]:
    """a plan's transaction, bound to the chain and account it was planned for (send_plan checks both)"""
    return {**call, "chainId": CHAIN_ID[chain], "from": sender}


def plan_add_liquidity(w3: Any, pool: dict[str, Any], *, owner: str, range_bp: int, amount0: Optional[int] = None, amount1: Optional[int] = None,
                       slippage_bps: int = DEFAULT_SLIPPAGE_BPS, native_side: Optional[int] = None, deadline_s: int = DEFAULT_DEADLINE_S) -> dict[str, Any]:
    """The approvals (exact amounts) and the mint for adding liquidity now. `pool` is the API's pool (client.pool(...)["pool"]).
    Give amount0 or amount1 (raw units); the other side is computed for the range at the current price."""
    chain, dex = pool["chain"], pool["dex"]
    npm = npm_of(chain, dex)
    if not npm:
        raise ValueError(f"adding liquidity is not supported for {dex} on {chain} (Uniswap v4: use the Uniswap app)")
    _assert_chain(w3, chain)
    if amount0 is None and amount1 is None:
        raise ValueError("give amount0 or amount1")
    sp, tick = _slot0(w3, pool["address"])
    lo, hi = range_ticks(tick, range_bp, pool["tickSpacing"])
    if amount1 is None:
        amount1 = other_amount(0, amount0, sp, lo, hi) or 0  # type: ignore[arg-type]
    if amount0 is None:
        amount0 = other_amount(1, amount1, sp, lo, hi) or 0
    amounts = mint_amounts(sp, lo, hi, amount0, amount1, slippage_bps)
    if amounts["liquidity"] == 0:
        raise ValueError("amount too small")
    deadline = int(time.time()) + deadline_s
    approvals = approvals_needed(chain, npm, [
        *([] if native_side == 0 else [{"token": pool["token0"], "amount": amount0, "allowance": _allowance(w3, pool["token0"], owner, npm)}]),
        *([] if native_side == 1 else [{"token": pool["token1"], "amount": amount1, "allowance": _allowance(w3, pool["token1"], owner, npm)}]),
    ])
    mint = mint_call(chain, dex, pool["token0"], pool["token1"], pool["fee"], pool["tickSpacing"], lo, hi, amounts, owner, deadline, native_side)
    return {"tickLower": lo, "tickUpper": hi, "amounts": amounts, "amount0": amount0, "amount1": amount1, "deadline": deadline,
            "approvals": [_bind(a, chain, owner) for a in approvals], "mint": _bind(mint, chain, owner)}


_POSITIONS_TYPES = ["uint96", "address", "address", "address", "int24", "int24", "int24", "uint128", "uint256", "uint256", "uint128", "uint128"]


def _multicall(w3: Any, calls: list[tuple[str, str]], block: Any = "latest") -> list[bytes]:
    """Multicall3.aggregate3, every call required to succeed."""
    data = _call("aggregate3((address,bool,bytes)[])", ["(address,bool,bytes)[]"], [[(_cs(t), False, bytes.fromhex(d[2:])) for t, d in calls]])
    (results,) = decode(["(bool,bytes)[]"], _eth_call(w3, MULTICALL3, data, block))
    return [r[1] for r in results]


def positions(w3: Any, chain: str, owner: str) -> list[dict[str, Any]]:
    """The wallet's positions on one chain's position managers that still hold liquidity or fees, each with its pool
    (from the manager's factory). Positions staked in a Slipstream gauge belong to the gauge and are not listed."""
    _assert_chain(w3, chain)
    # one snapshot for every read: NFTs moving between pages (ERC721Enumerable swaps the last one into a hole) could
    # otherwise hide a live position or fail the walk
    block = w3.eth.block_number
    out = []
    for dex, npm in NPM.get(chain, {}).items():
        (n,) = decode(["uint256"], _eth_call(w3, npm, _call("balanceOf(address)", ["address"], [_cs(owner)]), block))
        if n == 0:
            continue
        # every NFT the wallet holds, a page at a time (emptied positions are never burned: they keep their indexes,
        # so stopping at the first N could hide a live position behind them)
        ids, raw = [], []
        for start in range(0, n, _PAGE):
            part = [decode(["uint256"], r)[0] for r in _multicall(w3, [(npm, _call("tokenOfOwnerByIndex(address,uint256)", ["address", "uint256"], [_cs(owner), i])) for i in range(start, min(n, start + _PAGE))], block)]
            ids += part
            raw += [decode(_POSITIONS_TYPES, r) for r in _multicall(w3, [(npm, _call("positions(uint256)", ["uint256"], [i])) for i in part], block)]
        live = [(i, p) for i, p in zip(ids, raw) if p[7] > 0 or p[10] > 0 or p[11] > 0]
        if not live:
            continue
        (factory,) = decode(["address"], _eth_call(w3, npm, "0x" + _selector("factory()").hex(), block))
        sig = "getPool(address,address,int24)" if is_slipstream(dex) else "getPool(address,address,uint24)"
        typ = "int24" if is_slipstream(dex) else "uint24"
        pools = [decode(["address"], r)[0] for r in _multicall(w3, [(factory, _call(sig, ["address", "address", typ], [p[2], p[3], p[4]])) for _, p in live], block)]
        for (i, p), pl in zip(live, pools):
            out.append({"chain": chain, "dex": dex, "npm": npm, "pool": pl.lower(), "tokenId": i, "token0": p[2], "token1": p[3],
                        "feeOrSpacing": p[4], "tickLower": p[5], "tickUpper": p[6], "liquidity": p[7]})
    return out


def uncollected_fees(w3: Any, pos: dict[str, Any], owner: str) -> tuple[int, int]:
    """What collecting now would pay (fees owed, incl. those accrued since the last update): collect simulated."""
    raw = _eth_call(w3, pos["npm"], collect_call(pos["chain"], pos["dex"], pos["tokenId"], owner)["data"], sender=owner)
    a0, a1 = decode(["uint256", "uint256"], raw)
    return a0, a1


def plan_remove_liquidity(w3: Any, pos: dict[str, Any], *, owner: str, share_bps: int, slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
                          deadline_s: int = DEFAULT_DEADLINE_S, min_block: Optional[int] = None) -> dict[str, Any]:
    """One transaction: decrease `share_bps` of what the position holds NOW and collect it all — principal and fees — to
    the owner; 0 = fees only. Everything is read at one block no older than `min_block` (pass your previous removal's
    block from send_plan: a lagging RPC would return the old liquidity, and a repeated partial removal take more)."""
    _assert_chain(w3, pos["chain"])
    block = w3.eth.block_number
    if min_block is not None and block < min_block:
        raise StaleRead(block, min_block)
    (holder,) = decode(["address"], _eth_call(w3, pos["npm"], _call("ownerOf(uint256)", ["uint256"], [pos["tokenId"]]), block))
    if holder.lower() != owner.lower():
        raise ValueError(f"position {pos['tokenId']} is held by {holder}, not {owner}")
    now = decode(_POSITIONS_TYPES, _eth_call(w3, pos["npm"], _call("positions(uint256)", ["uint256"], [pos["tokenId"]]), block))
    sp, _ = _slot0(w3, pos["pool"], block)
    live = {**pos, "liquidity": now[7]}
    deadline = int(time.time()) + deadline_s
    if share_bps == 0 or live["liquidity"] == 0:
        a = {"liquidity": 0, "amount0": 0, "amount1": 0, "amount0Min": 0, "amount1Min": 0}
    else:
        a = remove_amounts(sp, live, share_bps, slippage_bps)
    return {**a, "deadline": deadline, "call": _bind(remove_call(pos["chain"], pos["dex"], pos["tokenId"], a, owner, deadline), pos["chain"], owner)}


def _wait_final(w3: Any, block: int, tx_hash: Any, h: str, timeout_s: float = 3600) -> None:
    """any failure here (RPC error, receipt gone after a reorg, no 'finalized' support, timeout) is TxPending with the hash"""
    until = time.time() + timeout_s
    try:
        while True:
            fin = w3.eth.get_block("finalized")
            if fin["number"] >= block:
                rc = w3.eth.get_transaction_receipt(tx_hash)
                if rc["blockNumber"] != block or w3.eth.get_block(block)["hash"] != rc["blockHash"]:
                    raise TxPending(h)  # reorged: unknown again
                # re-executed after a reorg it may have failed this time: only a final, canonical success counts
                if rc["status"] != 1:
                    raise TxReverted(h)
                return
            if time.time() > until:
                raise TxPending(h)
            time.sleep(6)
    except (TxPending, TxReverted):
        raise
    except Exception as e:
        raise TxPending(h) from e


class AccountBlocked(Exception):
    """After a send of unknown outcome (TxUnknown, TxPending) nothing more is sent from that account on that chain in
    this process until you have checked what became of it and call unblock() — another plan would read the same nonce."""

    def __init__(self, chain_id: int, account: str, reason: Exception):
        super().__init__(f"sending from {account} on chain {chain_id} is blocked after an unknown outcome ({reason}); check it, then call unblock({chain_id}, '{account}')")
        self.chain_id, self.account, self.reason = chain_id, account, reason


_blocked: dict[str, Exception] = {}


def unblock(chain_id: int, account: str) -> None:
    """You checked the transaction that made sending from this account unsafe: sending may go on."""
    _blocked.pop(f"{chain_id}:{account.lower()}", None)


_account_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _account_lock(chain_id: int, sender: str) -> threading.Lock:
    """plans of one account on one chain run one after another in this process (they would race for the same nonce)"""
    with _locks_guard:
        return _account_locks.setdefault(f"{chain_id}:{sender.lower()}", threading.Lock())


def send_plan(w3: Any, calls: list[dict[str, Any]], *, sender: str, timeout_s: float = 180, finalized: bool = False) -> list[dict[str, Any]]:
    """Sends a plan's calls in order from `sender` (your web3 must be able to sign for it, e.g. SignAndSendRawMiddlewareBuilder),
    each after the previous one's receipt. Every call must be for the chain web3 is on and planned for `sender` (refused
    otherwise). Returns each one's hash and block. Raises TxReverted, or TxPending if a receipt is not seen within
    `timeout_s` (a transaction cancelled or replaced in the wallet never yields this hash's receipt: TxPending, never a
    false success). `finalized`: also wait until each block is final and canonical (before another partial removal
    from the same position: a reorg could otherwise drop this one while the next reads the old liquidity)."""
    chain_id = calls[0]["chainId"] if calls else 0
    key = f"{chain_id}:{sender.lower()}"
    with _account_lock(chain_id, sender):
        if key in _blocked:
            raise AccountBlocked(chain_id, sender, _blocked[key])
        try:
            return _send_queued(w3, calls, sender=sender, timeout_s=timeout_s, finalized=finalized)
        except (TxUnknown, TxPending) as e:
            _blocked[key] = e
            raise


def _send_queued(w3: Any, calls: list[dict[str, Any]], *, sender: str, timeout_s: float, finalized: bool) -> list[dict[str, Any]]:
    sent = []
    for c in calls:
        cid = w3.eth.chain_id
        if c.get("chainId") != cid:
            raise ValueError(f"the call is for chain {c.get('chainId')}, web3 is on {cid}")
        if str(c.get("from", "")).lower() != sender.lower():
            raise ValueError(f"the call was planned for {c.get('from')}, not {sender}")
        # the planned chain goes into the transaction itself: the signature binds it (a provider that switched chains
        # since the check above cannot make it valid elsewhere)
        # the account's next nonce, pinned: the transaction uses exactly it, and a send failing without a hash says
        # where to look (a resend without checking could double the operation)
        nonce = w3.eth.get_transaction_count(_cs(sender), "pending")
        try:
            tx_hash = w3.eth.send_transaction({"from": _cs(sender), "to": _cs(c["to"]), "data": c["data"], "value": c["value"], "chainId": c["chainId"], "nonce": nonce})
        except Exception as e:
            raise TxUnknown(sender, nonce) from e
        h = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        try:
            rc = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_s)
        except Exception as e:  # web3 raises TimeExhausted
            raise TxPending(h) from e
        # finalized: the final canonical receipt decides, whatever the first one said (a first revert may re-run and
        # succeed after a reorg, and the other way round)
        if finalized:
            _wait_final(w3, rc["blockNumber"], tx_hash, h)
        elif rc["status"] != 1:
            raise TxReverted(h)
        sent.append({"hash": h, "blockNumber": rc["blockNumber"]})
    return sent

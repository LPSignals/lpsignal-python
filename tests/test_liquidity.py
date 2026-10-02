"""lpsignal.liquidity against the reference TypeScript implementation's vectors (tests/liquidity_vectors.json, generated
from the code the web app and the Node SDK run): every number and every calldata byte must match."""
import json
from pathlib import Path

import pytest

from lpsignal import liquidity as lp

V = json.loads((Path(__file__).parent / "liquidity_vectors.json").read_text(), object_hook=None)


def n(x):
    """vector values: bigints as 'n<digits>', lists/dicts recursively"""
    if isinstance(x, str) and x.startswith("n") and x[1:].lstrip("-").isdigit():
        return int(x[1:])
    if isinstance(x, list):
        return [n(i) for i in x]
    if isinstance(x, dict):
        return {k: n(v) for k, v in x.items()}
    return x


def test_tick_math():
    for tick, want in n(V["sqrt"]):
        assert lp.sqrt_ratio_at_tick(tick) == want
    assert lp.sqrt_ratio_at_tick(lp.MIN_TICK) == lp.MIN_SQRT_RATIO
    assert lp.sqrt_ratio_at_tick(lp.MAX_TICK) == lp.MAX_SQRT_RATIO
    with pytest.raises(ValueError):
        lp.sqrt_ratio_at_tick(lp.MAX_TICK + 1)


def test_ranges():
    for tick, bp, spacing, want in n(V["ranges"]):
        assert list(lp.range_ticks(tick, bp, spacing)) == want, (tick, bp, spacing)


def test_amounts():
    sp = lp.sqrt_ratio_at_tick(-200720)
    for side, amount, lo, hi, want in n(V["other"]):
        assert lp.other_amount(side, amount, sp, lo, hi) == want
    for d0, d1, s, want in n(V["mint"]):
        assert lp.mint_amounts(sp, -201210, -200230, d0, d1, s) == want
    pos = {"tickLower": -201210, "tickUpper": -200230, "liquidity": 10**18 + 7}
    for share, s, want in n(V["remove"]):
        assert lp.remove_amounts(sp, pos, share, s) == want
    with pytest.raises(ValueError):
        lp.remove_amounts(sp, pos, 0, 50)


def _same(call, want):
    assert call["to"].lower() == want["to"].lower()
    assert call["data"].lower() == want["data"].lower()
    assert call["value"] == want["value"]


def test_calldata_byte_for_byte():
    c = n(V["calls"])
    sp = lp.sqrt_ratio_at_tick(-200720)
    amounts = lp.mint_amounts(sp, -201210, -200230, 10**18, 1919892837, 50)
    me = "0x00000000000000000000000000000000000000aa"
    t0, t1 = "0x4200000000000000000000000000000000000006", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    _same(lp.mint_call("base", "uniswap_v3", t0, t1, 500, 10, -201210, -200230, amounts, me, 1_900_000_000, None), c["mint_v3"])
    _same(lp.mint_call("base", "uniswap_v3", t0, t1, 500, 10, -201210, -200230, amounts, me, 1_900_000_000, 0), c["mint_v3_native0"])
    _same(lp.mint_call("base", "aerodrome_cl", t0, t1, 500, 10, -201210, -200230, amounts, me, 1_900_000_000, None), c["mint_slipstream"])
    _same(lp.mint_call("bsc", "pancake_v3", "0x55d398326f99059ff775485246999027b3197955", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", 500, 10, -201210, -200230, amounts, me, 1_900_000_000, None), c["mint_pancake_bsc"])
    got = lp.approvals_needed("ethereum", "0x00000000000000000000000000000000000000bb", [
        {"token": "0xdac17f958d2ee523a2206206994597c13d831ec7", "amount": 10, "allowance": 3}, {"token": t0, "amount": 5, "allowance": 0}])
    assert len(got) == len(c["approvals_usdt"]) == 3
    for a, b in zip(got, c["approvals_usdt"]):
        _same(a, b)
    _same(lp.remove_call("base", "uniswap_v3", 42, {"liquidity": 5, "amount0Min": 1, "amount1Min": 2}, me, 1_900_000_000), c["remove"])
    _same(lp.remove_call("optimism", "velodrome_cl", 42, {"liquidity": 0, "amount0Min": 0, "amount1Min": 0}, me, 1_900_000_000), c["remove_fees"])


def test_refuses_what_cannot_be_minted():
    a = {"amount0Desired": 5, "amount1Desired": 7, "amount0Min": 4, "amount1Min": 6}
    me = "0x00000000000000000000000000000000000000aa"
    t0, t1 = "0x4200000000000000000000000000000000000006", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    with pytest.raises(ValueError, match="no position manager"):
        lp.mint_call("base", "uniswap_v4", t0, t1, 500, 10, -100, 100, a, me, 1, None)
    with pytest.raises(ValueError, match="grid"):
        lp.mint_call("base", "uniswap_v3", t0, t1, 500, 10, -95, 100, a, me, 1, None)
    with pytest.raises(ValueError, match="wrapped native"):
        lp.mint_call("base", "uniswap_v3", t0, t1, 500, 10, -100, 100, a, me, 1, 1)


class _Eth:
    def __init__(self, chain_id):
        self.chain_id = chain_id
        self.sent = []

    fail_send = False

    def get_transaction_count(self, who, tag):
        return 5

    def send_transaction(self, tx):
        self.sent.append(tx)
        if self.fail_send:
            raise ConnectionError("socket closed after the node accepted it")
        return bytes(32)

    def wait_for_transaction_receipt(self, h, timeout):
        return {"status": 1, "blockNumber": 7}

    def get_block(self, tag):
        raise ConnectionError("connection reset")


class _W3:
    def __init__(self, chain_id):
        self.eth = _Eth(chain_id)


def test_send_plan_refuses_another_chain_or_account():
    me, other = "0x00000000000000000000000000000000000000aa", "0x00000000000000000000000000000000000000bb"
    call = {"to": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1", "data": "0x01", "value": 0, "chainId": 8453, "from": me}
    w3 = _W3(10)
    with pytest.raises(ValueError, match="is for chain"):
        lp.send_plan(w3, [call], sender=me)
    w3 = _W3(8453)
    with pytest.raises(ValueError, match="planned for"):
        lp.send_plan(w3, [call], sender=other)
    assert w3.eth.sent == []


def test_send_plan_binds_the_chain_and_never_loses_the_hash():
    me = "0x00000000000000000000000000000000000000aa"
    call = {"to": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1", "data": "0x01", "value": 0, "chainId": 8453, "from": me}
    w3 = _W3(8453)
    assert lp.send_plan(w3, [call], sender=me) == [{"hash": "00" * 32, "blockNumber": 7}]
    assert w3.eth.sent[0]["chainId"] == 8453  # signed for the planned chain
    # finality: an RPC failure after the receipt is TxPending with the hash
    with pytest.raises(lp.TxPending) as e:
        lp.send_plan(w3, [call], sender=me, finalized=True)
    assert e.value.tx_hash == "00" * 32
    lp.unblock(8453, me)


class _Eth2(_Eth):
    """first receipt success; after a reorg the canonical receipt at the same height reverted"""

    def get_block(self, tag):
        return {"number": 9} if tag == "finalized" else {"hash": b"h"}

    def get_transaction_receipt(self, h):
        return {"blockNumber": 7, "blockHash": b"h", "status": 0}


def test_send_plan_final_receipt_must_succeed():
    me = "0x00000000000000000000000000000000000000aa"
    call = {"to": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1", "data": "0x01", "value": 0, "chainId": 8453, "from": me}
    w3 = _W3(8453)
    w3.eth = _Eth2(8453)
    with pytest.raises(lp.TxReverted):
        lp.send_plan(w3, [call, call], sender=me, finalized=True)
    assert len(w3.eth.sent) == 1  # the next call is never sent


def test_send_plan_unknown_send_outcome_names_the_nonce():
    me = "0x00000000000000000000000000000000000000aa"
    call = {"to": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1", "data": "0x01", "value": 0, "chainId": 8453, "from": me}
    w3 = _W3(8453)
    w3.eth.fail_send = True
    with pytest.raises(lp.TxUnknown) as e:
        lp.send_plan(w3, [call, call], sender=me)
    assert (e.value.sender, e.value.nonce) == (me, 5)
    assert len(w3.eth.sent) == 1 and w3.eth.sent[0]["nonce"] == 5
    # blocked until checked: nothing more is sent from that account on that chain
    w3.eth.fail_send = False
    with pytest.raises(lp.AccountBlocked):
        lp.send_plan(w3, [call], sender=me)
    assert len(w3.eth.sent) == 1
    lp.unblock(8453, me)
    assert len(lp.send_plan(w3, [call], sender=me)) == 1


class _Eth3(_Eth):
    """first receipt reverted; after a reorg the canonical one at the same height succeeded"""

    def wait_for_transaction_receipt(self, h, timeout):
        return {"status": 0, "blockNumber": 7}

    def get_block(self, tag):
        return {"number": 9} if tag == "finalized" else {"hash": b"h"}

    def get_transaction_receipt(self, h):
        return {"blockNumber": 7, "blockHash": b"h", "status": 1}


def test_send_plan_finalized_lets_the_final_receipt_decide():
    me = "0x00000000000000000000000000000000000000aa"
    call = {"to": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1", "data": "0x01", "value": 0, "chainId": 8453, "from": me}
    w3 = _W3(8453)
    w3.eth = _Eth3(8453)
    assert lp.send_plan(w3, [call], sender=me, finalized=True) == [{"hash": "00" * 32, "blockNumber": 7}]
    with pytest.raises(lp.TxReverted):
        lp.send_plan(w3, [call], sender=me)  # not finalized: the first receipt is the answer


def test_mint_minimums_never_revert_a_move_inside_the_tolerance():
    for tick, lo, hi in [(-200720, -201210, -200230), (-200720, -200760, -200680), (0, -10, 10), (-200720, -887270, 887270)]:
        sp, sa, sb = lp.sqrt_ratio_at_tick(tick), lp.sqrt_ratio_at_tick(lo), lp.sqrt_ratio_at_tick(hi)
        d0 = 10**18
        d1 = lp.other_amount(0, d0, sp, lo, hi)
        for slip in (10, 50, 100):
            m = lp.mint_amounts(sp, lo, hi, d0, d1, slip)
            for bps in range(-slip + 1, slip, max(1, slip // 7)):
                sp2 = sp * round((1 + bps / 1e4) ** 0.5 * 10**12) // 10**12
                if sp2 <= sa or sp2 >= sb:
                    continue
                l2 = lp.liquidity_for_amounts(sp2, sa, sb, m["amount0Desired"], m["amount1Desired"])
                u0, u1 = lp.amounts_for_liquidity(sp2, sa, sb, l2, True)
                assert u0 >= m["amount0Min"] and u1 >= m["amount1Min"], (tick, lo, hi, slip, bps)


class _NftEth(_Eth):
    """a wallet with 450 position NFTs on Base's Uniswap manager: all emptied but #449 (a live one past the first 400)"""

    def __init__(self):
        super().__init__(8453)
        self.block_number = 100
        self.blocks = set()

    def call(self, tx, block_identifier="latest"):
        self.blocks.add(block_identifier)
        from eth_abi import decode as dec, encode as enc
        data = bytes.fromhex(tx["data"][2:])
        sel = data[:4]
        if sel == lp._selector("balanceOf(address)"):
            return enc(["uint256"], [450 if tx["to"].lower() == lp.NPM["base"]["uniswap_v3"] else 0])
        if sel == lp._selector("factory()"):
            return enc(["address"], ["0x" + "fa" * 20])
        if sel == lp._selector("aggregate3((address,bool,bytes)[])"):
            (calls,) = dec(["(address,bool,bytes)[]"], data[4:])
            out = []
            for _, _, cd in calls:
                s, args = cd[:4], cd[4:]
                if s == lp._selector("tokenOfOwnerByIndex(address,uint256)"):
                    out.append((True, enc(["uint256"], [dec(["address", "uint256"], args)[1]])))
                elif s == lp._selector("positions(uint256)"):
                    tid = dec(["uint256"], args)[0]
                    out.append((True, enc(lp._POSITIONS_TYPES, [0, "0x" + "00" * 20, "0x" + "11" * 20, "0x" + "22" * 20, 500, -10, 10, 7 if tid == 449 else 0, 0, 0, 0, 0])))
                else:
                    out.append((True, enc(["address"], ["0x" + "33" * 20])))
            return enc(["(bool,bytes)[]"], [out])
        raise AssertionError(sel.hex())


def test_positions_walks_every_nft_not_just_the_first_page():
    w3 = _W3(8453)
    w3.eth = _NftEth()
    got = lp.positions(w3, "base", "0x00000000000000000000000000000000000000aa")
    assert [p["tokenId"] for p in got] == [449]
    assert w3.eth.blocks == {100}  # every read at one block: a single snapshot

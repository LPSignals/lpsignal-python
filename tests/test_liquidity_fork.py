"""lpsignal.liquidity against the real position managers, on local forks of each chain (anvil forked from a public
RPC). Opt-in: LP_FORK=1 pytest tests/test_liquidity_fork.py (needs Foundry's anvil). Public API only."""
import os
import subprocess
import time

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("LP_FORK") != "1", reason="set LP_FORK=1 (needs anvil)")

FORK = {"base": "https://base-rpc.publicnode.com", "optimism": "https://optimism-rpc.publicnode.com", "bsc": "https://bsc-rpc.publicnode.com",
        "ethereum": "https://ethereum-rpc.publicnode.com", "polygon": "https://polygon-bor-rpc.publicnode.com", "arbitrum": "https://arbitrum-one-rpc.publicnode.com"}
KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
CASES = [
    ("base", "0x6c561b446416e1a00e8e93e221854d6ea4171372", 500, True),   # Uniswap v3, paying ETH
    ("base", "0x70acdf2ad0bf2402c957154f944c19ef4e1cbae1", 500, False),  # Aerodrome Slipstream
    ("optimism", "0x478946bcd4a5a22b316470f5486fafb928c0ba25", 500, False),  # Velodrome Slipstream
    ("bsc", "0x4f31fa980a675570939b737ebdde0471a4be40eb", 5, False),  # PancakeSwap v3 stable
    ("ethereum", "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36", 500, False),  # WETH/USDT
    ("polygon", "0x50eaedb835021e4a108b7290636d62e9765cc6d7", 500, False),
]
_port = [18800]


@pytest.mark.parametrize("chain,address,range_bp,native", CASES)
def test_add_list_remove(chain, address, range_bp, native):
    from eth_abi import decode, encode
    from eth_utils import keccak
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware, SignAndSendRawMiddlewareBuilder

    from lpsignal import liquidity as lp

    pool = httpx.get(f"https://lpsignal.app/v1/pools/{chain}/{address}", timeout=30).json()["pool"]
    port = _port[0]; _port[0] += 1
    anvil = subprocess.Popen(["anvil", "--fork-url", FORK[chain], "--port", str(port), "--silent", "--no-rate-limit"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}", request_kwargs={"timeout": 60}))
        for _ in range(240):
            try:
                w3.eth.chain_id
                break
            except Exception:
                time.sleep(0.5)
        if chain in ("bsc", "polygon"):
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)  # proof-of-authority chains
        acct = w3.eth.account.from_key(KEY)
        w3.middleware_onion.inject(SignAndSendRawMiddlewareBuilder.build(acct), layer=0)
        me = acct.address
        sel = lambda s: keccak(text=s)[:4]
        bal = lambda t, who=me: decode(["uint256"], bytes(w3.eth.call({"to": Web3.to_checksum_address(t), "data": "0x" + (sel("balanceOf(address)") + encode(["address"], [who])).hex()})))[0]
        # fund the wallet from the pool's own balances (1/2000 of each)
        cp = Web3.to_checksum_address(address)
        w3.provider.make_request("anvil_impersonateAccount", [cp])
        w3.provider.make_request("anvil_setBalance", [cp, hex(10**18)])
        for t in (pool["token0"], pool["token1"]):
            amt = bal(t, cp) // 2000
            h = w3.eth.send_transaction({"from": cp, "to": Web3.to_checksum_address(t), "data": "0x" + (sel("transfer(address,uint256)") + encode(["address", "uint256"], [me, amt])).hex()})
            w3.eth.wait_for_transaction_receipt(h)
        w3.provider.make_request("anvil_stopImpersonatingAccount", [cp])
        native_side = (0 if pool["token0"].lower() == lp.WRAPPED[chain] else 1) if native else None

        plan = lp.plan_add_liquidity(w3, pool, owner=me, amount0=bal(pool["token0"]) // 2, range_bp=range_bp, native_side=native_side, deadline_s=10**7)
        if plan["amount1"] > bal(pool["token1"]):
            plan = lp.plan_add_liquidity(w3, pool, owner=me, amount1=bal(pool["token1"]) // 2, range_bp=range_bp, native_side=native_side, deadline_s=10**7)
        sent = lp.send_plan(w3, plan["approvals"] + [plan["mint"]], sender=me)
        assert len(sent) == len(plan["approvals"]) + 1

        mine = [p for p in lp.positions(w3, chain, me) if p["pool"] == address.lower()]
        assert len(mine) == 1
        pos = mine[0]
        assert (pos["tickLower"], pos["tickUpper"]) == (plan["tickLower"], plan["tickUpper"]) and pos["liquidity"] > 0
        assert len(lp.uncollected_fees(w3, pos, me)) == 2

        half = lp.plan_remove_liquidity(w3, pos, owner=me, share_bps=5000, deadline_s=10**7)
        assert half["liquidity"] == pos["liquidity"] // 2
        b0, b1 = bal(pool["token0"]), bal(pool["token1"])
        done = lp.send_plan(w3, [half["call"]], sender=me)
        assert bal(pool["token0"]) - b0 >= half["amount0Min"] and bal(pool["token1"]) - b1 >= half["amount1Min"]
        with pytest.raises(lp.StaleRead):
            lp.plan_remove_liquidity(w3, pos, owner=me, share_bps=10000, min_block=done[0]["blockNumber"] + 1000)
        rest = lp.plan_remove_liquidity(w3, pos, owner=me, share_bps=10000, deadline_s=10**7, min_block=done[0]["blockNumber"])
        assert rest["liquidity"] == pos["liquidity"] - half["liquidity"]
        lp.send_plan(w3, [rest["call"]], sender=me)
        assert not any(p["tokenId"] == pos["tokenId"] for p in lp.positions(w3, chain, me))
        with pytest.raises(ValueError, match="held by"):
            lp.plan_remove_liquidity(w3, pos, owner="0x00000000000000000000000000000000000000aa", share_bps=10000)
    finally:
        anvil.kill()


def test_swap_through_the_real_route():
    """plan_swap → send_plan on a Base fork: native ETH in, then WETH in (approval + swap): at least minReturn arrives,
    LPSignal's fee exactly (a fresh account: the aggregator refuses anvil's well-known default ones)."""
    from eth_abi import decode, encode
    from eth_utils import keccak
    from web3 import Web3
    from web3.middleware import SignAndSendRawMiddlewareBuilder

    from lpsignal import liquidity as lp

    address = "0x6c561b446416e1a00e8e93e221854d6ea4171372"
    pool = httpx.get(f"https://lpsignal.app/v1/pools/base/{address}", timeout=30).json()["pool"]
    port = _port[0]; _port[0] += 1
    anvil = subprocess.Popen(["anvil", "--fork-url", FORK["base"], "--port", str(port), "--silent", "--no-rate-limit"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}", request_kwargs={"timeout": 60}))
        for _ in range(240):
            try:
                w3.eth.chain_id
                break
            except Exception:
                time.sleep(0.5)
        acct = w3.eth.account.create()
        w3.middleware_onion.inject(SignAndSendRawMiddlewareBuilder.build(acct), layer=0)
        me = acct.address
        w3.provider.make_request("anvil_setBalance", [me, hex(10**21)])
        sel = lambda s: keccak(text=s)[:4]
        bal = lambda t, who: decode(["uint256"], bytes(w3.eth.call({"to": Web3.to_checksum_address(t), "data": "0x" + (sel("balanceOf(address)") + encode(["address"], [who])).hex()})))[0]
        w3.eth.wait_for_transaction_receipt(w3.eth.send_transaction({"from": me, "to": Web3.to_checksum_address(pool["token0"]), "value": 10**17, "data": "0x" + sel("deposit()").hex()}))
        fee_receiver = Web3.to_checksum_address(lp.SWAP_FEE_RECEIVER)
        for native in (True, False):
            amount_in = 10**17
            done = False
            for _ in range(4):  # re-planned on a revert: a fork freezes market makers' pools some routes go through
                plan = lp.plan_swap(w3, pool, owner=me, from_side=0, from_native=native, amount_in=amount_in)
                assert len(plan["approvals"]) == (0 if native else 1)
                fee0 = w3.eth.get_balance(fee_receiver) if native else bal(pool["token0"], fee_receiver)
                usdc0 = bal(pool["token1"], me)
                try:
                    lp.send_plan(w3, plan["approvals"] + [plan["swap"]], sender=me)
                except lp.TxReverted:
                    continue
                except lp.TxUnknown as e:
                    # the gas estimate reverted (certainly not sent): unblock and re-plan, as a user would
                    if "revert" not in str(e.__cause__).lower():
                        raise
                    lp.unblock(8453, me)
                    continue
                fee1 = w3.eth.get_balance(fee_receiver) if native else bal(pool["token0"], fee_receiver)
                assert fee1 - fee0 == amount_in * 25 // 10_000
                assert bal(pool["token1"], me) - usdc0 >= plan["minReturn"]
                done = True
                break
            assert done, "native" if native else "weth"
    finally:
        anvil.terminate()

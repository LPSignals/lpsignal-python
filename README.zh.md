# lpsignal（Python）

[LPSignal](https://lpsignal.app) 的官方 Python SDK。LPSignal 为蓝筹集中流动性池（Uniswap v3/v4、PancakeSwap v3、
Aerodrome / Velodrome Slipstream）提供扣除无常损失后的净 APR 信号，覆盖 Ethereum、BNB Chain、Base、Arbitrum、
Optimism 和 Polygon。

[English](README.md) · Node.js 版：[LPSignals/lpsignal-node](https://github.com/LPSignals/lpsignal-node) · [API 文档](https://lpsignal.app/docs)

```bash
pip install lpsignal
```

它提供：

- **REST API**：按净 APR 排序的池子、各区间指标、小时级历史、区间回测、信号及其 7 天后的实际结果、Smart LP
  排行榜、账户信息、Webhook 与 Telegram 设置、付费链接。
- **不漏信号的实时流**。服务端 WebSocket 重连时只补最近 24 小时；SDK 在每次连接前还会用 REST 拉取你最后一条信号之后
  的全部信号，所以停机多久都能补齐。信号按 id 顺序到达，进程运行期间每条只给一次。把最后一条 id 存下来（自带文件
  存储），重启后从断点继续；如果恰好在 handler 处理完、还没存盘时崩溃，这一条会再给一次，所以 handler 要按
  `signal.id` 做幂等。
- **Webhook 验签**：校验 `x-lpsignal-signature` 的 HMAC 和时间戳，返回解析后的内容。

## 关于 LPSignal

- [实时 LP 信号](https://lpsignal.app/zh/signals)及每条信号的实际结果
- [聪明 LP 排行榜](https://lpsignal.app/zh/smart-lps)：按已平仓头寸相对持币的收益给钱包排名
- [方法论](https://lpsignal.app/zh/methodology)：手续费、无常损失和区间回测怎么计算
- [集中流动性无常损失计算器](https://lpsignal.app/zh/calculator)
- [LPSignal 与 Revert、Metrix、Krystal、DefiLlama Yields 的对比](https://lpsignal.app/zh/compare)
- 各链最佳池子：[Ethereum](https://lpsignal.app/zh/chains/ethereum) · [BNB Chain](https://lpsignal.app/zh/chains/bsc) · [Base](https://lpsignal.app/zh/chains/base) · [Arbitrum](https://lpsignal.app/zh/chains/arbitrum) · [Optimism](https://lpsignal.app/zh/chains/optimism) · [Polygon](https://lpsignal.app/zh/chains/polygon)

## 快速开始

```python
import asyncio, os
from lpsignal import AsyncLPSignal, FileLastIdStore, SignalStream

async def main():
    lps = AsyncLPSignal(api_key=os.environ["LPSIGNAL_API_KEY"])
    page = await lps.pools(chain="base", pair_class="volatile", limit=10)
    for p in page["pools"]:
        print(p["pair"], p["dex"], f"{p['best']['netApr']:.1%}")

    def on_signal(signal, source):
        if signal["kind"] == "net_apr":
            print("开仓", signal["pair"], signal["tickLower"], signal["tickUpper"])

    await SignalStream(lps, on_signal, store=FileLastIdStore("lpsignal-state.json")).run()

asyncio.run(main())
```

## 单位约定

- APR 和比例都是小数：`0.345` 表示 34.5%。
- `ilApr` / `il7d` 是相对"直接持有两个币"的损失，所以**为 0 或负数**，`netApr = feeApr + ilApr`。
- `fee` 的单位是百分之一个基点：`500` 即 0.05% 费率档。
- `rangeBp` 是区间半宽，单位为价格的基点：`500` 即 ±5%，`0` 即全区间。实际要开的仓位是 `tickLower..tickUpper`，
  已按池子的 tick spacing 对齐。
- id 一律是字符串（可能超过 2^53），时间是 UTC 的 ISO 8601 字符串。

## 套餐

公开接口不需要 key，但机会信号要满 24 小时后才能看到。实时流、Webhook 和实时机会信号需要 Basic 或 Pro；Smart LP
信号和钱包持仓需要 Pro。套餐和完整 API 文档见 [lpsignal.app](https://lpsignal.app)。

## 推送哪些信号

信号流、webhook 和 Telegram 推送你订阅的类型：核心事件 `net_apr`、`tvl_outflow`、`depeg`、`smart_lp` 默认开启。
短时机会（`burst`：最近 1 小时净 APR 很高且有真实成交）需要添加后才推送——`setSubscriptions([...])`，或只对某个信号流
`new SignalStream({ ..., kinds: ['burst'] })`。

## 自定义规则

Basic（3 条规则）和 Pro（20 条规则）可以设置自己的阈值。命中只推送给你（信号流、webhook、Telegram），并带有
`signal.rule = { id, name }`。阈值和其他字段一样用小数表示。

```python
lps.create_rule({"kind": "tvl_outflow", "name": "big exits", "minDrop": 0.2, "windowHours": 6})
```

## 添加和移除流动性

`lpsignal.liquidity` 负责构造交易：在你选的价格区间内添加流动性，以及移除流动性。交易发到池子的官方仓位合约（Uniswap v3、PancakeSwap v3、Aerodrome 和 Velodrome Slipstream）。签名和发送都用你自己的 web3.py：私钥不经过 SDK；仓位只会创建到你自己的地址，资金也只领回到你自己的地址；中间没有 LPSignal 的合约，也不收费。这里不支持 Uniswap v4 池子。安装：`pip install "lpsignal[liquidity]"`。

用法见英文 README 的示例：`plan_add_liquidity` → `send_plan`，`positions` → `plan_remove_liquidity` → `send_plan`。BNB Chain 和 Polygon 需要在 web3 里加 `ExtraDataToPOAMiddleware`。

- 最低成交量按 Uniswap SDK 的规则计算，允许的价格变动由 `slippage_bps` 指定（默认 0.5%）；交易在 `deadline_s` 之后失效（默认 20 分钟）。
- 每笔计划好的交易都绑定了所属的链和账户：从别的账户或别的链发送，`send_plan` 会拒绝。
- `send_plan` 会等每一笔的回执。如果规定时间内没等到，会抛出带交易哈希的 `TxPending`：**在弄清楚这笔交易的结果之前，不要重发同一笔创建仓位或部分移除的交易**，否则第二笔也会成交。
- 发送时出错且拿不到交易哈希（节点可能已经收下了这笔交易）会抛出 `TxUnknown`，带上账户和 nonce：请先确认这个 nonce 有没有被用掉，再决定是否重发。
- 出现 `TxUnknown` 或 `TxPending` 后，这个账户在这条链上的后续发送都会抛出 `AccountBlocked`，直到你核实那笔交易后调用 `lp.unblock(chain_id, account)`。
- 对同一个仓位做下一次部分移除前：用 `finalized=True` 发送，并把上一次返回的区块号作为 `min_block` 传入。否则 RPC 节点落后或链重组时，下一次可能读到旧的流动性而多取。
- 最低成交量是价格在滑点范围两端时、仓位合约实际会收取的数量。如果区间比滑点范围还窄（例如稳定币池 ±0.05% 区间配 0.5% 滑点），两个最低值都可能是 0，这时交易在链上没有价格限制。但价格被推出你的区间时，添加只会变成存入单一代币（添加本身不做兑换）；价格回来时，转换只发生在你的区间内，所以损失上限是区间的宽度。
- 质押在 Aerodrome / Velodrome gauge 里的仓位属于 gauge，`positions` 不会列出。
- 不构成投资建议：过去收益好的区间，价格离开后也可能亏损。

## 兑换

`plan_swap` 通过 [KyberSwap](https://kyberswap.com) 聚合器，把池子里的一种代币兑换成另一种（例如添加前补足不够的那一边）。**LPSignal 收取输入金额的 0.25%（稳定型池 0.05%）作为手续费**，由聚合器路由合约直接转到 LPSignal 的地址。聚合器的返回不会被直接信任：报价必须接近池子自身的链上价格；它构造的交易会被解码核对，包括路由合约、代币和数量、收款人（你自己的地址）、手续费恰好是 LPSignal 的且没有其他费用、没有 permit、保证的最少到账不低于你的滑点允许值，然后再模拟一次。路由合约保证至少到账 `minReturn`，否则交易回滚。

用法：`plan_swap(w3, pool, owner=..., from_side=0, amount_in=..., from_native=False, to_native=False, slippage_bps=50, min_out=None)` → `send_plan(w3, plan["approvals"] + [plan["swap"]], sender=...)`。

- `min_out`：这次兑换至少要到账的数量（例如你缺的数量）；报价扣除滑点后不够时会拒绝（`SwapRefused` `moved`）。其他拒绝原因：`impact`（报价比池子价格低太多）、`quote` / `calldata`（聚合器返回与请求不符）、`simulation`（现在发送会失败）。
- 计划生成后请立即发送（报价会变；`deadline_s` 后失效，默认 10 分钟）。兑换的截止时间写在无法核对的 calldata 里，所以遇到 `TxUnknown` / `TxPending` 后，请先弄清那笔交易本身的结果再兑换（期间 `send_plan` 会锁住该账户）。
- 不支持 Uniswap v4 池子。聚合器会拒绝部分地址（例如公开的测试私钥）。

## 开发

```bash
pip install -e '.[test]' && pytest    # 单元测试
```

用 [`testdata/webhook-vectors.json`](testdata/webhook-vectors.json) 校验 webhook 签名，这份向量是用
LPSignal 服务端自己的签名函数生成的（Node 与 Python 两个仓库共用同一份）。

对运行中的 API 做端到端测试（默认只读；`E2E_WRITE=1` 会调用写接口，只能用测试账号）：

```bash
LPSIGNAL_BASE_URL=https://api.lpsignal.app LPSIGNAL_API_KEY=lps_... python scripts/e2e.py
```

## 许可证

[MIT](LICENSE)

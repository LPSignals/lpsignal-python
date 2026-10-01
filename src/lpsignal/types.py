"""Response shapes of the LPSignal API, as returned (camelCase keys, JSON values).

- APRs and ratios are fractions: 0.345 = 34.5%.
- `ilApr` / `il7d` are the loss versus holding, so they are <= 0, and `netApr = feeApr + ilApr`.
- `fee` is in hundredths of a basis point: 500 = the 0.05% fee tier.
- `rangeBp` is the range half-width in basis points of price: 500 = ±5%, 0 = full range.
- Ids and 256-bit values are strings; timestamps are ISO 8601 strings in UTC.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict, Union


class ChainStatus(TypedDict):
    chain: str
    block: str
    blockTs: str
    firstBlockTs: str
    lagSec: int
    activePools: int


class BestRange(TypedDict):
    windowHours: int
    rangeBp: int
    tickLower: int
    tickUpper: int
    exact: bool
    feeApr: float
    ilApr: float
    netApr: float
    inRangeRatio: float
    stakedEmissionApr: float
    asOf: str


class RankedPool(TypedDict):
    chain: str
    address: str
    dex: str
    pair: str
    fee: int
    pairClass: str
    tvlUsd: float
    tvlAt: Optional[str]
    best: BestRange


Order = Literal["asc", "desc"]
# pools: the best range's figures, or the pool's TVL / fee tier
PoolSort = Literal["netApr", "feeApr", "ilApr", "inRange", "emissionApr", "tvl", "fee"]
# smart LPs: `rank` (the pnl rank) stays each wallet's rank whatever the sort
BoardSort = Literal["rank", "pnl", "return", "capital", "closes", "wins"]
SignalSort = Literal["time", "return", "outcome"]
OpenSort = Literal["lastEvent", "openedAt", "entryUsd"]
ClosedSort = Literal["closedAt", "openedAt", "capitalUsd", "pnlUsd"]


class PoolsPage(TypedDict):
    pools: list[RankedPool]
    limit: int
    offset: int
    total: int
    sort: str
    order: str


class PoolDetail(TypedDict):
    pool: dict[str, Any]
    metrics: list[dict[str, Any]]


class PoolHour(TypedDict):
    hour: str
    swaps: int
    volume0: float
    volume1: float
    fee0PerL: float
    fee1PerL: float
    openTick: int
    closeTick: int
    minTick: int
    maxTick: int
    closeLiquidity: str
    feeExact: bool


class Backtest(TypedDict):
    chain: str
    address: str
    rangeBp: int
    days: int
    asOf: str
    feeApr: float
    ilApr: float
    netApr: float
    inRangeRatio: float
    uncertainFeeApr: float
    startTick: int
    endTick: int
    tickLower: int
    tickUpper: int
    feesExact: bool
    exact: bool


class SignalOutcome(TypedDict):
    status: Literal["done", "inexact"]
    netApr: Optional[float]
    feeApr: Optional[float]
    ilApr: Optional[float]
    evaluatedAt: str


# A signal carries the common fields below plus its kind's own fields (see the README):
#   net_apr:     tvlUsd, net24h, net7d, net30d, fee7d, il7d, inRange7d, stakedEmissionApr?
#   tvl_outflow: tvlBeforeUsd, tvlNowUsd, drop, windowHours
#   depeg:       deviation, severe, medianTick, tick
#   smart_lp:    owner, tokenId, entryUsd, top, rank, wallet30d
# A signal as returned. `kind`: net_apr | burst (short-term: netApr, feeApr, ilApr, inRangeRatio over windowHours, swaps) | tvl_outflow |
# depeg | smart_lp. `rule` is {"id", "name"} when one of your custom rules produced it (private to you, never scored),
# else None.
Signal = dict[str, Any]


class SignalsPage(TypedDict, total=False):
    signals: list[Signal]
    next: Optional[str]
    # sorted by return / outcome only
    total: int
    offset: int
    sort: str
    order: str


# rank, owner, positions, wins, capitalUsd, pnlUsd, returnPct, chains
LeaderboardWallet = dict[str, Any]


class Leaderboard(TypedDict):
    windowDays: int
    full: bool
    wallets: list[LeaderboardWallet]
    total: int  # wallets on the board for this caller (non-Pro: at most 10)
    limit: int
    offset: int
    sort: str
    order: str


class WalletPositions(TypedDict):
    owner: str
    open: list[dict[str, Any]]
    closed: list[dict[str, Any]]
    openTotal: int
    closedTotal: int
    limit: int
    openOffset: int
    closedOffset: int


class Follow(TypedDict):
    owner: str
    since: str


class Me(TypedDict):
    id: str
    tier: Literal["free", "basic", "pro"]
    paid: bool
    telegramLinked: bool
    webhookUrl: Optional[str]
    walletAddress: Optional[str]
    hasApiKey: bool
    # kinds of global signal pushed to this account (Telegram, webhook, WebSocket without `kinds`): the core events
    # net_apr, tvl_outflow, depeg, smart_lp by default; burst only when added. Custom-rule matches always arrive.
    subscriptions: list[str]


# A custom alert rule as stored (every default filled in): id, kind ("net_apr" | "depeg" | "tvl_outflow"), name,
# enabled, active, chains, pairClasses, pools, minTvlUsd, cooldownHours and the thresholds of its kind
# (net_apr: minNet7d, minNet24h, minInRange7d; depeg: minDeviation; tvl_outflow: minDrop, windowHours).
# Thresholds are fractions: 0.15 = 15%.
Rule = dict[str, Any]


class RulesPage(TypedDict):
    rules: list[Rule]
    limit: int
    dailyCap: int
    matchesToday: int


class WebhookRegistration(TypedDict):
    url: str
    secret: str


class TelegramLink(TypedDict):
    code: str
    instruction: str


BillingStatus = dict[str, Any]
Json = Union[dict[str, Any], list[Any]]

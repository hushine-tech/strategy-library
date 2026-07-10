# Strategy Library

> 更新时间：2026-07-10

## 概述

Strategy Library 是 Hushine 发布的 Python 策略 SDK 及共享兼容库。它提供策略声明与校验、本地确定性 replay、策略可见的钱包类型、市场数据读取、指标算法，以及 Elemental 兼容的日志与 tracing；它本身不是交易执行服务。

---

## 发布 SDK：`hushine_strategy`

- `hushine_strategy.types`：策略声明、`OrderDecision`、枚举和回调使用的数据类型。
- `hushine_strategy.validator`：平台与本地工具共享的策略声明 / 代码校验。
- `hushine_strategy.replay`：由 strategy-debugger-cli 使用的确定性本地 replay。
- `hushine_strategy.wallet`：策略可见的钱包类型和 helper；生产记账 runtime 仍由 strategy-service 维护。

`hushine_strategy` 顶层包会重新导出常用声明与类型，包括 `StrategyInput`、`StrategyOrderTarget`、`OrderDecision`、`OrderSide`、`OrderType` 和 `PositionSide`。

---

## 顶层共享模块

### `market_data`（市场数据模型与读取）

```python
from market_data import BacktestDataSource, LiveDataSource
```

| 类 | 说明 |
|---|---|
| BacktestDataSource | 从 TimescaleDB 读取历史数据 |
| LiveDataSource | 从 Kafka 消费实时数据 |
| models | MarketKline / MarketOI / MarketFunding 数据模型 |

---

### `algo`（指标算法与 bundle）

```python
from algo.indicators import RSI, MACD, BollingerBands, ATR
from algo import IndicatorBundle
```

| 指标 | 说明 |
|---|---|
| RSI | 相对强弱指数 |
| MACD | 指数平滑异同移动平均线 |
| BollingerBands | 布林带 |
| ATR | 平均真实波幅 |
| SMA / EMA | 简单/指数移动平均 |
| CCI | 顺势指标 |
| DMI | 方向移动指数 |
| IndicatorBundle | 组合指标封装 |

---

### `utils.log`（日志与 tracing）

```python
from utils.log import get_logger
```

`utils.log` 提供 Python Elemental 兼容的结构化日志、gRPC interceptor 和 OpenTelemetry tracing。`utils.middleware` 下还保留共享的 gRPC / Kafka middleware 封装。

---

## 依赖关系

```
Strategy Service
    -> Strategy Library
        -> hushine_strategy（策略声明、校验、replay、策略可见钱包）
        -> market_data（数据来源）
        -> algo（指标计算）
        -> utils.log（日志、tracing）

strategy-debugger-cli
    -> hushine_strategy.validator / replay

core-service order.v1
    -> 订单执行与持久化
```

生产钱包记账与 session 执行属于 strategy-service。真实订单统一通过 core-service `order.v1` 执行，不属于本库职责。

---

## 状态

| 模块 | 状态 |
|---|---|
| hushine_strategy.types | ✅ 策略声明、订单决策与枚举 |
| hushine_strategy.validator | ✅ 平台 / 本地共享校验 |
| hushine_strategy.replay | ✅ strategy-debugger-cli 确定性 replay |
| hushine_strategy.wallet | ✅ 策略可见 wallet helper；不承担生产记账 |
| market_data | ✅ BacktestDataSource + LiveDataSource，已集成至 strategy_service.data_loop |
| algo | ✅ RSI / MACD / BB / ATR / SMA / EMA / CCI / DMI + Bundle |
| utils.log | ✅ Elemental 兼容日志、gRPC interceptor 与 tracing |

# Strategy Library

> 更新时间：2026-04-04

## 概述

Strategy Library 是 Strategy Service 的基础库，不能独立运行。提供市场数据接口、指标算法、日志和中间件封装。

钱包 runtime 已在 `Phase C2b` 后收敛到 `strategy-service` 仓库内维护，
本库不再保留独立的 wallet 实现或兼容导出。

---

## 模块说明

### market_data（市场数据接口）

```python
from market_data import BacktestDataSource, LiveDataSource
```

| 类 | 说明 |
|---|---|
| BacktestDataSource | 从 TimescaleDB 读取历史数据 |
| LiveDataSource | 从 Kafka 消费实时数据 |
| models | MarketKline / MarketOI / MarketFunding 数据模型 |

---

### algo（指标算法）

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

### wallet（已移出本库）

旧的 `Position / FutureWallet / Account / SpotWallet` 实现已经删除。

- 当前唯一有效的钱包 runtime 位于 `strategy-service/strategy_service/wallet/`
- `strategy-service` 主链路统一通过
  `strategy_service.wallet_factory.build_wallet_from_account` 构造运行时
- 本库现在只保留 `market_data`、`algo`、`utils` 这三类通用能力

---

### utils（基础工具）

```python
from utils.log import get_logger
from utils.middleware.grpc import GRPCClientMiddleware
from utils.middleware.kafka import KafkaConsumer
```

| 模块 | 说明 |
|---|---|
| log | Python 日志中间件（对齐 elemental 格式） |
| middleware.grpc | gRPC 客户端中间件（自动记录 RPC 日志） |
| middleware.kafka | Kafka 消费端中间件 |

---

## 依赖关系

```
Strategy Service
    -> Strategy Library
        -> market_data（数据来源）
        -> algo（指标计算）
        -> utils（日志、gRPC、Kafka）
```

---

## 状态

| 模块 | 状态 |
|---|---|
| market_data | ✅ BacktestDataSource + LiveDataSource，已集成至 strategy_service.data_loop |
| algo | ✅ RSI / MACD / BB / ATR / SMA / EMA / CCI / DMI + Bundle |
| utils | ✅ 日志 + gRPC客户端 + Kafka消费端 |



---

## 待开发

- [ ] gRPC Scraper 控制接口（控制 Scraper 启动/停止，向 Kafka 写入数据）
- [ ] 真实 Broker 订单服务（替换当前 Mock）

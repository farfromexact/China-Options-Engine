# China Options Engine

中国股指期货、股指期权的日频数据和衍生指标层，覆盖 IH、IF、IC、IM 与 HO、IO、MO，并补充对应现金指数与代表性 ETF 的公开市场数据。

## 数据产物

- `data/latest.json`：最新完整期权链、逐合约数据、IV、Greeks、期指及现金市场联动。
- `data/radar_latest.json`：供每日雷达读取的最新紧凑快照。
- `data/radar_history.json`：按交易日整理的紧凑历史，供 Automation 和 Dashboard 做多期比较。
- `data/snapshots/YYYY-MM-DD.json`：可审计、可回填的完整历史快照。
- `data/last_run_status.json`：最近一次采集和联动状态。

`radar_history.json` 只接纳 `data_fresh=true`、期权官方 EOD 与期指官方数据均成功且交易日一致的快照，默认保留最近 60 个交易日。同一交易日重复运行时按日期替换，不产生重复记录；记录按日期升序排列。重建时从新到旧收集到 60 个合格交易日即停止，旧版或 EOD 前半成品会跳过，损坏 JSON 和快照文件名/日期错位仍会显式报错。节假日或数据源失败时不会制造虚假的新交易日。

每条历史记录包括：

- HO、IO、MO 最近四个到期月份的 ATM IV、25/10 Delta wings、RR25、BF25、PCR、成交、持仓和前三 Gamma 节点；
- IH、IF、IC、IM 主力与下一合约、涨跌、成交、持仓和期限结构；
- 自部署后的现金指数收盘、现金基差及代表 ETF 份额/净申赎估算；
- 期指—期权同月份联动和 forward 差异；
- 数据新鲜度、官方覆盖率、期指数据状态和错误清单。

## 现金指数、真基差与 ETF 份额层

`futures_link.py` 在完成 CFFEX 期指解析后，会调用 `cash_market.py` 从零鉴权公开数据源补充：

- `IH` ↔ 上证50（000016）↔ 50ETF（510050）；
- `IF` ↔ 沪深300（000300）↔ 300ETF（510300）；
- `IC` ↔ 中证500（000905）↔ 500ETF（510500）；
- `IM` ↔ 中证1000（000852）↔ 中证1000ETF（512100）。

当前公开源：

- 腾讯公开行情接口：现金指数及 ETF 的收盘/最新价、前收、涨跌、成交额与时间戳；
- 东方财富公开 quote 接口：代表 ETF 的总份额（优先 `f84`，缺失时显式回退 `f85`）。

这些数据写入 `futures.cash_market`，同时每个期指产品摘要增加：

- `cash_index_close`、`cash_index_change_pct`；
- `cash_basis_points`、`cash_basis_pct`；
- `annualized_cash_basis_pct_inferred`；
- `reference_etf_total_shares`、`reference_etf_share_change`、`reference_etf_share_change_pct`；
- `reference_etf_estimated_net_creation_redemption_cny`。

这里的 `cash_basis` 才是“期指收盘价 - 对应现金指数收盘价”的真实现金基差；年化值只按主力合约日历 DTE 线性年化，**未做分红和融资成本调整**，不得误称为理论 fair-value basis。

ETF “资金流”采用：

`当日总份额变化 × ETF 当日市场收盘价`

它是 **一级市场净申赎规模估算**，不是二级市场逐笔资金流；由于 ETF 申赎可包含实物证券，不能表述为精确现金进出。旧快照没有 ETF 份额字段时，部署后的第一个成功交易日只记录总份额，从第二个成功交易日起才会自然产生 `share_change` 与估算净申赎。

公开现金市场源被临时封锁或不可用时，该层降级为 `partial/missing` 并记录错误，但不会破坏已经验证成功的 CFFEX 期权和期指日终产物。

## 历史更新与回填

日常工作流在 `futures_link.py` 完成后从已验证 snapshots 确定性重建：

```powershell
python radar_history.py
```

只校验已提交的历史文件：

```powershell
python radar_history.py --check
```

首次启用历史比较时，可以从最新 verified snapshot 向前回填 20 个交易日。默认语义是“现有锚点之前 20 个交易日”，因此锚点也计入后会得到至少 21 条记录，足以计算严格的 20 交易日变化：

```powershell
# 先下载、计算、暂存并校验，不发布文件
python backfill_cffex.py --dry-run

# 校验通过后发布 snapshots 并重建 radar_history.json
python backfill_cffex.py
python radar_history.py --check
```

回填器优先按月份下载一次 CFFEX 历史 ZIP 并在内存中复用，只有月包不可用时才回退到单日 CSV。所有目标日期会先在临时目录完成计算和验证，数量不足或任一交易日数据不完整时不会发布。默认不覆盖已有 verified snapshot；只有显式传入 `--overwrite` 才允许替换。

历史 CSV 不包含当时的 bid/ask 和盘口深度，因此 History 的 forward、IV、RR/BF 与 Gamma 统一使用 CFFEX 官方 EOD 结算价（无正结算价时回退正收盘价）重算。回填器也会为现有锚点补入独立的 `history_products`，保留原始实时盘口产品数据；后续日跑会继续生成同口径的 History 指标。`radar_latest.json` 仍保留当前盘口口径。

现金市场字段采用向后兼容的 history schema：现有旧历史记录继续合法；下一次从 snapshots 重建后，每个期指摘要都会带现金指数/基差/ETF字段。部署前 snapshot 没有这些字段时对应值保持 `null`，不会伪造成 0；因此严格的 1/3/5/20 日现金基差或 ETF 份额比较，应从实际存在非空数据的日期开始计算。

每条 History 记录都会明确写入 `data_quality.record_origin` 和 `data_quality.option_price_basis`。官方结算价口径的机器可读值为 `cffex_official_settlement_fallback_close`，不应把它误称为历史实时盘口中间价。

网络会默认忽略机器上的 `HTTP_PROXY` / `HTTPS_PROXY`，避免失效的本地代理拖慢 CFFEX 请求。确实需要使用环境代理时设置 `CFFEX_TRUST_ENV=true`；公共现金市场层同理可通过 `PUBLIC_MARKET_TRUST_ENV=true` 显式允许环境代理。

## 消费端读取顺序

每日雷达建议固定读取：

1. `data/radar_latest.json`：当前状态，包括 `futures.cash_market`、现金基差与代表 ETF 份额/流量估算；
2. `data/radar_history.json`：1、3、5、20 个交易日比较；新现金市场字段同样在每个期指摘要内保留；
3. `data/latest.json`：需要逐执行价或逐合约细节时；
4. `data/snapshots/YYYY-MM-DD.json`：审计、复核或历史重建时。

历史期权记录应按 `symbol` 连接，不能只按“近月”位置连接，以免换月时把不同合约误当成连续序列。现金指数和 ETF 份额层自部署日起进入 dated snapshot 和后续重建的 `radar_history.json`；部署前缺失值必须保持 `null`。

## 验证

```powershell
python -m unittest discover -s tests -v
python radar_history.py --check
```

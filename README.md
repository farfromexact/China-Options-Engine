# China Options Engine

中国股指期货、股指期权的日频数据和衍生指标层，覆盖 IH、IF、IC、IM 与 HO、IO、MO。

## 数据产物

- `data/latest.json`：最新完整期权链、逐合约数据、IV 和 Greeks。
- `data/radar_latest.json`：供每日雷达读取的最新紧凑快照。
- `data/radar_history.json`：按交易日整理的紧凑历史，供 Automation 和 Dashboard 做多期比较。
- `data/snapshots/YYYY-MM-DD.json`：可审计、可回填的完整历史快照。
- `data/last_run_status.json`：最近一次采集和联动状态。

`radar_history.json` 只接纳 `data_fresh=true`、期权官方 EOD 与期指官方数据均成功且交易日一致的快照，默认保留最近 60 个交易日。同一交易日重复运行时按日期替换，不产生重复记录；记录按日期升序排列。重建时从新到旧收集到 60 个合格交易日即停止，旧版或 EOD 前半成品会跳过，损坏 JSON 和快照文件名/日期错位仍会显式报错。节假日或数据源失败时不会制造虚假的新交易日。

每条历史记录包括：

- HO、IO、MO 最近四个到期月份的 ATM IV、25/10 Delta wings、RR25、BF25、PCR、成交、持仓和前三 Gamma 节点；
- IH、IF、IC、IM 主力与下一合约、涨跌、成交、持仓和期限结构；
- 期指—期权同月份联动和 forward 差异；
- 数据新鲜度、官方覆盖率、期指数据状态和错误清单。

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

每条 History 记录都会明确写入 `data_quality.record_origin` 和 `data_quality.option_price_basis`。官方结算价口径的机器可读值为 `cffex_official_settlement_fallback_close`，不应把它误称为历史实时盘口中间价。

网络会默认忽略机器上的 `HTTP_PROXY` / `HTTPS_PROXY`，避免失效的本地代理拖慢 CFFEX 请求。确实需要使用环境代理时设置 `CFFEX_TRUST_ENV=true`。

## 消费端读取顺序

每日雷达建议固定读取：

1. `data/radar_latest.json`：当前状态；
2. `data/radar_history.json`：1、3、5、20 个交易日比较；
3. `data/latest.json`：需要逐执行价或逐合约细节时；
4. `data/snapshots/YYYY-MM-DD.json`：审计、复核或历史重建时。

历史期权记录应按 `symbol` 连接，不能只按“近月”位置连接，以免换月时把不同合约误当成连续序列。

## 验证

```powershell
python -m unittest discover -s tests -v
python radar_history.py --check
```

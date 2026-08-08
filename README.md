# China Options Engine

中国股指期货、股指期权的日频数据和衍生指标层，覆盖 IH、IF、IC、IM 与 HO、IO、MO。

## 数据产物

- `data/latest.json`：最新完整期权链、逐合约数据、IV 和 Greeks。
- `data/radar_latest.json`：供每日雷达读取的最新紧凑快照。
- `data/radar_history.json`：按交易日整理的紧凑历史，供 Automation 和 Dashboard 做多期比较。
- `data/snapshots/YYYY-MM-DD.json`：可审计、可回填的完整历史快照。
- `data/last_run_status.json`：最近一次采集和联动状态。

`radar_history.json` 只接纳 `data_fresh=true`、期权官方 EOD 与期指官方数据均成功且交易日一致的快照，默认保留最近 60 个交易日。同一交易日重复运行时按日期替换，不产生重复记录；记录按日期升序排列。节假日、EOD 前半成品或数据源失败时不会制造虚假的新交易日。

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

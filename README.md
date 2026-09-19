# QuantLearning — 聚宽（JoinQuant）策略学习 Demo

基于聚宽 SDK 的量化策略示例集。目标：把**可复用的公共函数**抽取到 `common` 包，
**每个策略一个文件、一个入口函数**，方便学习和复用。

## 目录结构

```
JoinQuant/
├── common/                 # 公共可复用函数
│   ├── indicators.py       # 技术指标（纯 pandas，本地可测试）
│   ├── metrics.py          # 绩效指标（纯函数）
│   └── utils.py            # 交易辅助（选股/下单/止损止盈/仓位）
└── strategies/             # 每个策略一个文件
    ├── template.py         # 策略骨架模板
    ├── dual_ma.py          # 双均线（金叉/死叉）
    ├── bollinger.py        # 布林带（均值回归）
    ├── rsi.py              # RSI 超买超卖
    ├── momentum.py         # 动量轮动（多标的）
    └── small_cap.py        # 小市值（多因子）
```

## 策略一览

| 文件 | 策略 | 类型 | 标的 | 调仓频率 |
|------|------|------|------|----------|
| `dual_ma.py` | 双均线金叉/死叉 | 趋势跟踪 | 单股 | 每日 |
| `bollinger.py` | 布林带均值回归 | 均值回归 | 单股 | 每日 |
| `rsi.py` | RSI 超买超卖 | 摆动指标 | 单股 | 每日 |
| `momentum.py` | 动量轮动 | 趋势跟踪 | 多股 | 每月 |
| `small_cap.py` | 小市值选股 | 多因子 | 多股 | 每月 |

## 怎么运行

**本地结构说明**：本仓库用 Python 包组织代码，方便复用与维护。
`common/indicators.py` 和 `common/metrics.py` 是纯函数，本地即可用：

```bash
python3 -c "from JoinQuant.common.indicators import ma, rsi; print(ma.__name__, rsi.__name__)"
```

**在聚宽网页回测端运行**：聚宽回测是"一个回测一个脚本"，不识别本仓库的包导入。
运行某个策略时，需要把该策略文件连同它用到的 `common` 函数**合并成单个文件**再粘贴，
或使用聚宽的「本地库 / 研究」上传模块功能。

每个策略文件顶部注释已列出它 `from JoinQuant.common.xxx import ...` 用到的函数，
合并时把这些函数复制进策略文件即可。例如 `dual_ma.py` 用到了：

- `common/indicators.py` 的 `ma`
- `common/utils.py` 的 `close_position`、`stop_loss_check`

## 关键 API 速查

- `initialize(context)`：初始化，设置基准/滑点/手续费/定时任务
- `run_daily / run_weekly / run_monthly`：定时任务
- `attribute_history(sec, count, '1d', ['close'], df=True)`：取历史 K 线
- `history(count, '1d', 'close', stocks, df=True)`：批量取多只股票历史
- `get_index_stocks(index)` / `get_fundamentals(query(...))`：选股
- `order_value / order_target / order_target_value`：下单
- `context.portfolio.positions`：持仓；`context.portfolio.available_cash`：可用资金

## 下一步建议

- 给策略加参数回测（不同均线周期、RSI 阈值）
- 增加滑点/手续费敏感性测试
- 把 `after_trading` 里的复盘日志补全（记录每日净值、持仓）

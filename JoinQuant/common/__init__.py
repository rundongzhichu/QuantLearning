# -*- coding: utf-8 -*-
"""公共可复用函数包。

- indicators.py: 技术指标（纯 Python/pandas，不依赖聚宽，可本地测试）
- metrics.py:    绩效指标（纯函数）
- utils.py:      依赖聚宽运行时的交易辅助函数（选股、下单、止损止盈、仓位管理）

策略文件（strategies/*.py）通过
    from JoinQuant.common.indicators import ...
    from JoinQuant.common.utils import ...
复用这里的能力。
"""

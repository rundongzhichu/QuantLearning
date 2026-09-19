# -*- coding: utf-8 -*-
"""策略包。

每个策略一个文件、一个独立的入口函数体系（initialize + 定时任务），
符合聚宽「一个回测一个脚本」的规范，可独立复制到聚宽网页回测端运行。

现有策略：
- template.py   策略骨架模板（新策略从它开始）
- dual_ma.py    双均线（金叉/死叉）
- bollinger.py  布林带（均值回归）
- rsi.py        RSI 超买超卖
- momentum.py   动量轮动（多标的）
- small_cap.py  小市值（多因子选股）

运行说明见项目根目录 README.md。
"""

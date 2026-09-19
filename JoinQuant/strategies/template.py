# -*- coding: utf-8 -*-
"""
策略骨架模板（新策略从这里复制）。

聚宽一个策略 = initialize(context) + 若干定时任务函数。常用定时点：
- run_daily(func, time='before_open')   开盘前：准备股票池、参数
- run_daily(func, time='open')          开盘时：下单
- run_daily(func, time='every_bar')     每个 bar（日线回测即每天一次）
- run_daily(func, time='after_close')   收盘后：复盘、记录
- run_weekly(func, weekday=1, time='open')   每周第一个交易日
- run_monthly(func, monthday=1, time='open') 每月第一个交易日
"""
from jqdata import *


def initialize(context):
    # 1. 设置基准（用于对比收益）
    set_benchmark('000300.XSHG')
    # 2. 使用真实价格（动态复权，避免回测未来函数）
    set_option('use_real_price', True)
    # 3. 设置滑点
    set_slippage(FixedSlippage(0.02))
    # 4. 设置手续费 / 印花税
    set_order_cost(
        OrderCost(open_tax=0, close_tax=0.001,
                  open_commission=0.0003, close_commission=0.0003,
                  close_today_commission=0, min_commission=5),
        type='stock',
    )
    # 5. 减少 order 日志噪音（可选）
    log.set_level('order', 'error')

    # 6. 注册定时任务
    run_daily(before_trading, time='before_open', reference_security='000300.XSHG')
    run_daily(market_open, time='open', reference_security='000300.XSHG')
    run_daily(after_trading, time='after_close', reference_security='000300.XSHG')


def before_trading(context):
    """开盘前：准备今日股票池、参数。"""
    pass


def market_open(context):
    """开盘时：实际下单逻辑。"""
    pass


def after_trading(context):
    """收盘后：记录、复盘。"""
    pass

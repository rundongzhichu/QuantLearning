# -*- coding: utf-8 -*-
"""
布林带策略（均值回归）。

逻辑：
- 收盘价跌破下轨（超卖）→ 买入
- 收盘价突破上轨（超买）→ 卖出
- 布林带默认 20 日均线 ± 2 倍标准差

适用场景：震荡市均值回归效果较好；强趋势中会过早离场。
回测频率建议：日。
"""
from jqdata import *
from JoinQuant.common.indicators import boll
from JoinQuant.common.utils import close_position


def initialize(context):
    g.stock = '600519.XSHG'      # 标的：贵州茅台
    g.window = 20                # 布林带周期
    g.num_std = 2.0              # 标准差倍数
    g.buy_cash_ratio = 0.95      # 买入资金占比

    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_order_cost(
        OrderCost(open_tax=0, close_tax=0.001,
                  open_commission=0.0003, close_commission=0.0003,
                  close_today_commission=0, min_commission=5),
        type='stock',
    )

    run_daily(trade, time='every_bar')


def trade(context):
    security = g.stock

    df = attribute_history(security, g.window + 5, '1d',
                           ['close'], skip_paused=True, df=True)
    if len(df) < g.window + 1:
        return
    close = df['close']
    mid, upper, lower = boll(close, g.window, g.num_std)

    price = close.iloc[-1]
    holding = security in context.portfolio.positions

    if price < lower.iloc[-1] and not holding:      # 跌破下轨 → 买入
        value = context.portfolio.available_cash * g.buy_cash_ratio
        order_value(security, value)
        log.info('跌破下轨买入 %s，价格 %.2f' % (security, price))
    elif price > upper.iloc[-1] and holding:        # 突破上轨 → 卖出
        close_position(context, security)
        log.info('突破上轨卖出 %s，价格 %.2f' % (security, price))

# -*- coding: utf-8 -*-
"""
RSI 超买超卖策略。

逻辑：
- RSI 跌破超卖阈值（如 30）→ 买入
- RSI 突破超买阈值（如 70）→ 卖出
- RSI 在 0~100 之间，反映近期涨跌力量强弱

适用场景：震荡市；趋势市中 RSI 会长期钝化在超买/超卖区。
回测频率建议：日。
"""
from jqdata import *
from JoinQuant.common.indicators import rsi
from JoinQuant.common.utils import close_position


def initialize(context):
    g.stock = '000858.XSHE'      # 标的：五粮液
    g.window = 14                # RSI 周期
    g.oversold = 30              # 超卖阈值
    g.overbought = 70            # 超买阈值
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
    rsi_value = rsi(df['close'], g.window).iloc[-1]

    holding = security in context.portfolio.positions

    if rsi_value < g.oversold and not holding:       # 超卖 → 买入
        value = context.portfolio.available_cash * g.buy_cash_ratio
        order_value(security, value)
        log.info('RSI=%.2f 超卖买入 %s' % (rsi_value, security))
    elif rsi_value > g.overbought and holding:       # 超买 → 卖出
        close_position(context, security)
        log.info('RSI=%.2f 超买卖出 %s' % (rsi_value, security))

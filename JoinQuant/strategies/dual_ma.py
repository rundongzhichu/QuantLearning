# -*- coding: utf-8 -*-
"""
双均线策略（金叉买入、死叉卖出）—— 趋势跟踪入门。

逻辑：
- 短期均线（如 5 日）上穿长期均线（如 20 日）→ 金叉，买入
- 短期均线下穿长期均线 → 死叉，卖出
- 配合固定比例止损控制风险

适用场景：单边趋势行情；震荡市会频繁假信号。
回测频率建议：日。
"""
from jqdata import *
from JoinQuant.common.indicators import ma
from JoinQuant.common.utils import close_position, stop_loss_check


def initialize(context):
    g.stock = '000001.XSHE'      # 标的：平安银行
    g.short_window = 5           # 短期均线周期
    g.long_window = 20           # 长期均线周期
    g.buy_cash_ratio = 0.95      # 买入时使用可用资金的占比
    g.stop_loss = 0.05           # 止损比例

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

    # 取足够长的收盘价序列计算两条均线
    df = attribute_history(security, g.long_window + 5, '1d',
                           ['close'], skip_paused=True, df=True)
    if len(df) < g.long_window + 1:
        return
    close = df['close']
    short_ma = ma(close, g.short_window)
    long_ma = ma(close, g.long_window)

    # 用最新两根 K 线判断是否发生金叉/死叉
    cur_short, cur_long = short_ma.iloc[-1], long_ma.iloc[-1]
    prev_short, prev_long = short_ma.iloc[-2], long_ma.iloc[-2]
    gold_cross = prev_short <= prev_long and cur_short > cur_long
    death_cross = prev_short >= prev_long and cur_short < cur_long

    holding = security in context.portfolio.positions

    # 先止损（风险控制优先）
    if holding and stop_loss_check(context, security, g.stop_loss):
        return

    if gold_cross and not holding:
        value = context.portfolio.available_cash * g.buy_cash_ratio
        order_value(security, value)
        log.info('金叉买入 %s' % security)
    elif death_cross and holding:
        close_position(context, security)
        log.info('死叉卖出 %s' % security)

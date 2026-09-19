# -*- coding: utf-8 -*-
"""
动量轮动策略（多标的，月度调仓）。

逻辑：
- 从沪深300成分股中，按过去 N 日涨幅排序，买入涨幅最大的前 top_n 只
- 每月第一个交易日调仓：卖出掉出榜单的，等权买入新榜单
- "强者恒强" 假设：过去表现好的股票未来继续跑赢

适用场景：趋势市中动量因子有效；震荡市会追高被套。
回测频率建议：日（run_monthly 会在每月第一个交易日触发）。
"""
from jqdata import *
from JoinQuant.common.utils import filter_stocks


def initialize(context):
    g.index = '000300.XSHG'      # 股票池来源：沪深300
    g.period = 20                # 动量计算期（过去 N 日涨幅）
    g.top_n = 10                 # 持有股票数量
    g.rebalance_day = 1          # 每月第 1 个交易日调仓

    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_order_cost(
        OrderCost(open_tax=0, close_tax=0.001,
                  open_commission=0.0003, close_commission=0.0003,
                  close_today_commission=0, min_commission=5),
        type='stock',
    )

    run_monthly(rebalance, monthday=g.rebalance_day, time='open')


def rebalance(context):
    # 1. 获取并过滤股票池
    stocks = filter_stocks(get_index_stocks(g.index))
    if len(stocks) == 0:
        return

    # 2. 计算每只股票过去 N 日涨幅
    #    history 返回 DataFrame，行为时间、列为股票
    df = history(g.period, '1d', 'close', stocks, df=True, skip_paused=True)
    if df is None or df.empty:
        return
    returns = df.iloc[-1] / df.iloc[0] - 1.0   # 涨幅序列（按股票索引）
    returns = returns.dropna().sort_values(ascending=False)

    # 3. 目标股票 = 涨幅前 top_n
    target = list(returns.index[:g.top_n])

    # 4. 卖出不在目标榜单中的持仓
    for security in list(context.portfolio.positions.keys()):
        if security not in target:
            order_target(security, 0)
            log.info('调出 %s' % security)

    # 5. 等权买入目标股票
    per_cash = context.portfolio.total_value / max(len(target), 1)
    for security in target:
        order_target_value(security, per_cash)

    log.info('本月动量组合：%s' % ','.join(target))

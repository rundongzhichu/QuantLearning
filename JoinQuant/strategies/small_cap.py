# -*- coding: utf-8 -*-
"""
小市值策略（多因子选股，月度调仓）。

逻辑：
- 从中证1000成分股中，按流通市值从小到大排序，买入最小的 top_n 只
- 每月第一个交易日调仓
- 依据 A 股历史"小市值溢价"：小盘股长期有超额收益

注意：小市值因子在 2017 年后大幅回撤，且易受退市/壳价值变化影响，仅作学习示例。
回测频率建议：日。
"""
from jqdata import *
from JoinQuant.common.utils import filter_stocks


def initialize(context):
    g.index = '000852.XSHG'      # 股票池来源：中证1000
    g.top_n = 20                 # 持有股票数量
    g.rebalance_day = 1          # 每月第 1 个交易日调仓

    set_benchmark('000905.XSHG')  # 基准：中证500
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

    # 2. 用 query + get_fundamentals 按流通市值升序取前 top_n
    q = (
        query(valuation.code)
        .filter(valuation.code.in_(stocks))
        .order_by(valuation.circulating_market_cap.asc())
        .limit(g.top_n)
    )
    df = get_fundamentals(q)
    if df is None or df.empty:
        return
    target = list(df['code'])

    # 3. 卖出不在目标榜单中的持仓
    for security in list(context.portfolio.positions.keys()):
        if security not in target:
            order_target(security, 0)
            log.info('调出 %s' % security)

    # 4. 等权买入目标股票
    per_cash = context.portfolio.total_value / max(len(target), 1)
    for security in target:
        order_target_value(security, per_cash)

    log.info('本月小市值组合（%d 只）：%s' % (len(target), ','.join(target)))

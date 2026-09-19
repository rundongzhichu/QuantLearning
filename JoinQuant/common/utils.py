# -*- coding: utf-8 -*-
"""
交易辅助函数（依赖聚宽运行时）。

注意：这里的 get_current_data / order_target / log / get_index_stocks 等
都是聚宽回测环境注入的全局对象，只有把代码放到聚宽回测端运行才有效。
本文件仅做组织与复用，本地（无聚宽环境）无法执行这些函数。
"""
import numpy as np


# ------------------------- 股票池筛选 -------------------------

def filter_stocks(stock_list):
    """过滤停牌、ST、退市整理等不可交易股票，返回可交易列表。"""
    current_data = get_current_data()
    result = []
    for stock in stock_list:
        cd = current_data[stock]
        if cd.paused:              # 停牌
            continue
        if cd.is_st:               # ST / *ST
            continue
        if '退' in cd.name:        # 退市整理
            continue
        result.append(stock)
    return result


def get_index_stock_list(index_symbol='000300.XSHG'):
    """获取指数成分股并过滤不可交易股票。index_symbol 如 '000300.XSHG'（沪深300）。"""
    return filter_stocks(get_index_stocks(index_symbol))


# ------------------------- 下单辅助 -------------------------

def close_position(context, security):
    """清空某只股票的持仓（只卖可卖数量）。"""
    position = context.portfolio.positions.get(security)
    if position is not None and position.closeable_amount > 0:
        order_target(security, 0)


def close_all_positions(context):
    """清空全部持仓。"""
    for security in list(context.portfolio.positions.keys()):
        close_position(context, security)


def buy_by_value(context, security, value):
    """按金额买入（金额太小则跳过，避免不足一手报错）。"""
    if value < 100:
        return
    order_value(security, value)


# ------------------------- 仓位管理 -------------------------

def position_percent(context, security):
    """返回某股票市值占总资产的比例（0~1）。"""
    position = context.portfolio.positions.get(security)
    if position is None or position.total_amount == 0:
        return 0.0
    return position.value / context.portfolio.total_value


# ------------------------- 止损止盈 -------------------------

def stop_loss_check(context, security, stop_loss_ratio=0.05):
    """固定比例止损：亏损超过阈值则清仓。返回是否触发。"""
    position = context.portfolio.positions.get(security)
    if position is None or position.total_amount == 0:
        return False
    profit_ratio = (position.price - position.avg_cost) / position.avg_cost
    if profit_ratio <= -stop_loss_ratio:
        close_position(context, security)
        log.info('止损卖出 %s，亏损 %.2f%%' % (security, profit_ratio * 100))
        return True
    return False


def take_profit_check(context, security, take_profit_ratio=0.10):
    """固定比例止盈：盈利超过阈值则清仓。返回是否触发。"""
    position = context.portfolio.positions.get(security)
    if position is None or position.total_amount == 0:
        return False
    profit_ratio = (position.price - position.avg_cost) / position.avg_cost
    if profit_ratio >= take_profit_ratio:
        close_position(context, security)
        log.info('止盈卖出 %s，盈利 %.2f%%' % (security, profit_ratio * 100))
        return True
    return False


def trailing_stop_check(context, security, tracker, ratio=0.08):
    """移动止损：从持仓期间最高价回撤超过 ratio 则清仓。

    tracker 需为 dict（{security: 最高价}），通常挂在 g 上，如 g.highest_price。
    """
    position = context.portfolio.positions.get(security)
    if position is None or position.total_amount == 0:
        return False
    current_price = position.price
    highest = tracker.get(security, current_price)
    if current_price > highest:
        highest = current_price
        tracker[security] = highest
    drawdown = (current_price - highest) / highest
    if drawdown <= -ratio:
        close_position(context, security)
        log.info('移动止损卖出 %s，回撤 %.2f%%' % (security, abs(drawdown) * 100))
        return True
    return False

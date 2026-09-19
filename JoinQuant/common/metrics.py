# -*- coding: utf-8 -*-
"""绩效指标（纯函数，输入为净值序列，本地可测试）。"""
import pandas as pd
import numpy as np


def max_drawdown(nav):
    """最大回撤（负数，如 -0.15 表示回撤 15%）。nav 为净值序列。"""
    nav = pd.Series(nav)
    return float((nav / nav.cummax() - 1.0).min())


def annualized_return(nav, periods_per_year=252):
    """年化收益率。"""
    nav = pd.Series(nav)
    total = nav.iloc[-1] / nav.iloc[0]
    years = (len(nav) - 1) / periods_per_year
    if years <= 0:
        return 0.0
    return float(total ** (1.0 / years) - 1.0)


def sharpe_ratio(nav, periods_per_year=252, risk_free=0.0):
    """夏普比率。"""
    nav = pd.Series(nav)
    returns = nav.pct_change().dropna()
    if len(returns) == 0 or returns.std() == 0:
        return 0.0
    return float((returns.mean() - risk_free) / returns.std() * np.sqrt(periods_per_year))


def win_rate(trade_returns):
    """胜率。trade_returns 为每笔交易的收益率序列。"""
    arr = np.asarray(trade_returns, dtype=float)
    if len(arr) == 0:
        return 0.0
    return float((arr > 0).mean())

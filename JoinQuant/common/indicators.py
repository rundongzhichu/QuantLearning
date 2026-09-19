# -*- coding: utf-8 -*-
"""
技术指标计算（纯函数，只依赖 pandas / numpy，不依赖聚宽 API）。

这样做的意义：指标逻辑可以在本地直接单元测试，不依赖聚宽回测环境。
所有函数输入均为 pandas Series（价格序列），输出为 Series 或元组。

用法示例（本地）：
    import pandas as pd
    close = pd.Series([...])
    ma5 = ma(close, 5)
"""
import pandas as pd
import numpy as np


def ma(series, window):
    """简单移动平均 SMA。"""
    return series.rolling(window).mean()


def ema(series, window):
    """指数移动平均 EMA。"""
    return series.ewm(span=window, adjust=False).mean()


def macd(close, fast=12, slow=26, signal=9):
    """MACD 指标。返回 (DIF, DEA, 柱状图 hist)。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def rsi(close, window=14):
    """相对强弱指标 RSI（Wilder 平滑，alpha=1/window）。返回 0~100 的序列。"""
    diff = close.diff()
    gain = diff.clip(lower=0.0)
    loss = (-diff).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def boll(close, window=20, num_std=2.0):
    """布林带。返回 (中轨 mid, 上轨 upper, 下轨 lower)。"""
    mid = close.rolling(window).mean()
    std = close.rolling(window).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


def atr(high, low, close, window=14):
    """平均真实波幅 ATR（用于止损/仓位管理）。"""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(window).mean()


def kdj(high, low, close, n=9, m1=3, m2=3):
    """KDJ 随机指标。返回 (K, D, J)。"""
    lowest = low.rolling(n).min()
    highest = high.rolling(n).max()
    rsv = (close - lowest) / (highest - lowest) * 100.0
    k = rsv.ewm(alpha=1.0 / m1, adjust=False).mean()
    d = k.ewm(alpha=1.0 / m2, adjust=False).mean()
    j = 3.0 * k - 2.0 * d
    return k, d, j


def roc(close, window=12):
    """变动率 ROC（动量因子，单位 %）。"""
    prev = close.shift(window)
    return (close - prev) / prev * 100.0

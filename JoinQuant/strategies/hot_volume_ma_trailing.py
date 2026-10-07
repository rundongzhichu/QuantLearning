# -*- coding: utf-8 -*-
"""
热度 + 放量 + 均线选股策略（沪深A股，参照沪深300指数择时）—— 移动止盈版。

【选股条件】（全部满足，才能进入合格池）
1. 放量：最近5日平均成交量 >= 之前5日平均成交量的 2 倍（即增加100%）
2. 形态：5日均线拐头向上，或处于上升趋势（收盘价 > MA20 且 MA5 > MA20）
3. 板块：所属申万一级行业最近10日涨幅排名前10
4. 热度：按“热度”排序取前3只候选（换手率优先，成交额兜底）

【择时】（参照沪深300指数）
- 下跌趋势 或 缩量      → 空仓
- 上涨趋势 / 放量 / 震荡 → 开仓，总仓位上限 60%
- 沪深300 当日涨幅 > 1.5% → 仓位降到 30%

【仓位管理】
- 单只股票 <= 20%，总仓位 <= 60%（即最多 3 只）
- 跌破选股条件（不在合格池）即严格止损卖出；指数空仓时清仓
- 持仓 < 3 只：按热度从高到低买入候选补齐

【卖出（盘中实时触发，立即卖出）】
1. 移动止盈：相对持仓成本涨 > 5% 后，从动态峰值回落 2% → 立即卖出全部可卖
2. 严格止损：跌破选股条件即卖出

【交易约束】
- 每只票一日只交易一次：开盘建仓/止损，盘中只触发一次移动止盈
- 移动止盈后 3 天内禁止买入

说明：本文件自包含，不依赖 JoinQuant.common 等公共模块。
盘中移动止盈为分钟级，回测请选“分钟”频率；日线回测下退化为每天触发一次。
"""
import numpy as np
import pandas as pd
from jqdata import *


# ------------------------------ 参数 ------------------------------

VOL_SURGE = 2.0          # 放量倍数：近5日均量 >= 前5日均量 * 2（增加100%）
MA_SHORT = 5             # 短期均线
MA_MID = 20              # 中期均线
MA_LONG = 60             # 长期均线
CANDIDATE_N = 3          # 候选数量
MAX_POSITIONS = 3        # 最大持仓数量
MAX_SINGLE = 0.20        # 单只股票仓位上限 20%
MAX_EXPOSURE = 0.60      # 总仓位上限 60%
REDUCED_EXPOSURE = 0.30  # 指数大涨时降到的仓位 30%
INDEX_SURGE = 0.015      # 沪深300 单日涨幅阈值 1.5%
SECTOR_TOP_N = 10        # 板块涨幅排名前 N
SECTOR_DAYS = 10         # 板块涨幅统计窗口
INDEX_CODE = '000300.XSHG'

# 止盈 / 止损参数
TAKE_PROFIT_ARM = 0.05    # 以成本价为基准，盈利 > 5% 后启动移动止盈
TRAILING_PULLBACK = 0.02  # 从动态峰值回落 2% 立即止盈
TP_COOLDOWN_DAYS = 3      # 移动止盈后 N 天内禁止买入


def initialize(context):
    set_benchmark(INDEX_CODE)
    set_option('use_real_price', True)
    set_slippage(FixedSlippage(0.02))
    set_order_cost(
        OrderCost(open_tax=0, close_tax=0.001,
                  open_commission=0.0003, close_commission=0.0003,
                  close_today_commission=0, min_commission=5),
        type='stock',
    )
    log.set_level('order', 'error')

    g.candidates = []       # 今日候选（合格池里热度前3，从高到低）
    g.qualified = set()     # 今日合格池（通过全部选股条件的股票）
    g.exposure = 0.0        # 今日目标总仓位
    g.target_holdings = []  # 今日目标持仓
    g.peak_price = {}       # 持仓动态峰值（移动止盈用，跨日保持）
    g.tp_date = {}          # 移动止盈卖出日期 → 冷却期内禁止买入

    run_daily(before_trading, time='before_open', reference_security=INDEX_CODE)
    run_daily(market_open, time='open', reference_security=INDEX_CODE)
    run_daily(after_trading, time='after_close', reference_security=INDEX_CODE)


def before_trading(context):
    """开盘前：计算择时信号、合格池、候选池。"""
    g.exposure = index_exposure(context)
    g.candidates = []
    g.qualified = set()
    g.target_holdings = []

    # 清理过期的止盈冷却记录
    today = context.current_dt.date()
    if g.tp_date:
        g.tp_date = {s: d for s, d in g.tp_date.items()
                     if (today - d).days <= TP_COOLDOWN_DAYS}

    if g.exposure <= 0:
        log.info('指数空仓信号（下跌趋势或缩量）')
        return

    universe = get_a_share_universe(context)
    qualified = pick_candidates(context, universe)   # 全部合格股票（按热度排序）
    g.qualified = set(qualified)
    g.candidates = qualified[:CANDIDATE_N]

    log.info('合格 %d 只，候选 %d 只：%s' %
             (len(g.qualified), len(g.candidates), ','.join(g.candidates)))


def market_open(context):
    """开盘时：严格止损卖出（跌破条件）；不足3只按热度补齐；新进直接建满单只目标仓位。"""
    total = context.portfolio.total_value
    positions = context.portfolio.positions
    holding = [s for s in positions if positions[s].total_amount > 0]

    # 空仓信号：清掉所有持仓
    if g.exposure <= 0:
        for s in holding:
            order_target_value(s, 0)
        if holding:
            log.info('空仓，清仓 %s' % ','.join(holding))
        g.target_holdings = []
        return

    qualified = g.qualified
    candidates = g.candidates

    # 1) 严格止损：跌破选股条件（不在合格池）即卖出
    for s in holding:
        if s not in qualified:
            order_target_value(s, 0)
            g.peak_price.pop(s, None)
            log.info('跌破选股条件卖出（止损） %s' % s)

    # 2) 确定目标持仓：剩余合格持仓 + 热度最高的候选补齐，最多3只
    keep = [s for s in holding if s in qualified]
    if len(keep) > MAX_POSITIONS:                 # 正常不会发生，仅兜底
        rank = {c: i for i, c in enumerate(candidates)}
        keep.sort(key=lambda s: rank.get(s, 999))
        keep = keep[:MAX_POSITIONS]
    today = context.current_dt.date()
    for s in candidates:
        if len(keep) >= MAX_POSITIONS:
            break
        if s in keep:
            continue
        if s in g.tp_date and (today - g.tp_date[s]).days <= TP_COOLDOWN_DAYS:
            continue   # 移动止盈冷却期内不重新买入
        keep.append(s)

    g.target_holdings = keep
    if not keep:
        return

    # 3) 开盘仓位：单只上限 = min(20%, 总仓位/3)；新进直接建满，日内不再加仓
    single_target = min(MAX_SINGLE, g.exposure / MAX_POSITIONS)
    for s in keep:
        pos = positions.get(s)
        cur_ratio = pos.value / total if pos is not None else 0.0
        if pos is not None and cur_ratio > 0:
            # 已有持仓：只降不升（指数降仓位时下调）
            if cur_ratio > single_target:
                order_target_value_with_tolerance(context, s, total * single_target)
        else:
            # 新进标的：直接建满单只目标仓位
            order_target_value_with_tolerance(context, s, total * single_target)

    log.info('目标持仓 %s，单只上限 %.0f%%'
             % (','.join(keep), single_target * 100))


def handle_data(context, data):
    """盘中（每个bar）：移动止盈——相对成本涨 >5% 后，从峰值回落 2% 立即全部卖出。"""
    if g.exposure <= 0:
        return
    positions = context.portfolio.positions
    current = get_current_data()
    today = context.current_dt.date()

    for s in list(positions.keys()):
        pos = positions[s]
        if pos.total_amount <= 0:
            continue
        cd = current[s]
        if cd.paused:
            continue
        try:
            bar = data[s]
        except Exception:
            continue
        if bar is None:
            continue

        last = getattr(bar, 'close', 0.0) or 0.0
        if last <= 0:
            continue

        # 更新跨日动态峰值（移动止盈用）
        b_high = getattr(bar, 'high', 0.0) or last
        peak = max(g.peak_price.get(s, 0.0), b_high)
        g.peak_price[s] = peak

        cost = pos.avg_cost or 0.0

        # 移动止盈：相对成本涨 >5% 后，从峰值回落 2% 立即卖出全部可卖
        if cost > 0 and peak > cost * (1 + TAKE_PROFIT_ARM) \
                and last <= peak * (1 - TRAILING_PULLBACK):
            closeable = pos.closeable_amount
            if closeable > 0:
                order(s, -closeable)
                log.info('移动止盈立即卖出 %s，成本 %.2f 峰值 %.2f 现价 %.2f'
                         % (s, cost, peak, last))
                g.peak_price.pop(s, None)
                g.tp_date[s] = today   # 记录止盈日期，冷却期内禁止买入


def after_trading(context):
    """收盘后：记录持仓与仓位。"""
    positions = context.portfolio.positions
    weight = sum(p.value for p in positions.values()) / context.portfolio.total_value
    log.info('持仓 %d 只，实际总仓位 %.1f%%' % (len(positions), weight * 100))


# ------------------------------ 择时 ------------------------------

def index_exposure(context):
    """参照沪深300：返回目标总仓位（0 / 30% / 60%）。"""
    df = attribute_history(INDEX_CODE, MA_LONG + 5, '1d',
                           ['close', 'volume'], skip_paused=True, df=True)
    if df is None or len(df) < MA_LONG + 1:
        return 0.0

    close = df['close']
    vol = df['volume']

    ma5 = close.rolling(MA_SHORT).mean()
    ma20 = close.rolling(MA_MID).mean()
    ma60 = close.rolling(MA_LONG).mean()

    # 下跌趋势：空头排列（MA5 < MA20 < MA60）
    trend_down = (ma5.iloc[-1] < ma20.iloc[-1]) and (ma20.iloc[-1] < ma60.iloc[-1])

    # 量能：近5日均量 vs 之前5日均量
    vol5 = vol.iloc[-5:].mean()
    vol_prev5 = vol.iloc[-10:-5].mean()
    vol_shrinking = vol_prev5 > 0 and vol5 < vol_prev5      # 缩量
    vol_expanding = vol_prev5 > 0 and vol5 > vol_prev5      # 放量

    # 当日涨幅
    day_ret = close.iloc[-1] / close.iloc[-2] - 1

    if trend_down or vol_shrinking:
        log.info('指数：下跌趋势=%s 缩量=%s → 空仓' % (trend_down, vol_shrinking))
        return 0.0
    if day_ret > INDEX_SURGE:
        log.info('指数当日涨幅 %.2f%% > 1.5%%，仓位降至 30%%' % (day_ret * 100))
        return REDUCED_EXPOSURE
    # 上涨 / 放量 / 震荡 → 开仓
    log.info('指数：放量=%s 涨幅 %.2f%% → 开仓' % (vol_expanding, day_ret * 100))
    return MAX_EXPOSURE


# ------------------------------ 选股 ------------------------------

def get_a_share_universe(context):
    """获取沪深A股股票池（排除ST/退市、上市不足60日、已退市、停牌）。"""
    df = get_all_securities(['stock'], date=context.previous_date)
    current = get_current_data()
    codes = []
    for code in df.index:
        if code[0] not in '036':        # 仅沪深A股（排除B股/北交所等）
            continue
        info = df.loc[code]
        name = str(info.display_name)
        if 'ST' in name or '退' in name:
            continue
        start = info.start_date
        if start is not None and (context.previous_date - start).days < 60:
            continue
        if info.end_date is not None and info.end_date < context.previous_date:
            continue
        if code in current and current[code].paused:
            continue
        codes.append(code)
    return codes


def pick_candidates(context, universe):
    """按 放量 -> 均线形态 -> 板块 -> 热度 层层筛选，返回全部合格股票（按热度从高到低排序）。"""
    if not universe:
        return []

    price = get_price(universe, end_date=context.previous_date,
                      count=MA_LONG + 5, fields=['close', 'volume', 'money'],
                      panel=False, skip_paused=True)
    if price is None or len(price) == 0:
        return []

    p = price.reset_index()  # 列：time, code, close, volume, money
    close = p.pivot_table(index='time', columns='code', values='close')
    vol = p.pivot_table(index='time', columns='code', values='volume')
    money = p.pivot_table(index='time', columns='code', values='money')

    # ---- 1) 放量：近5日均量 >= 前5日均量 * 2 ----
    vol5 = vol.iloc[-5:].mean()
    vol_prev5 = vol.iloc[-10:-5].mean()
    vol_ratio = vol5 / vol_prev5.replace(0, np.nan)
    surge = vol_ratio >= VOL_SURGE

    # ---- 2) 均线拐头向上 或 上升趋势 ----
    ma5 = close.rolling(MA_SHORT).mean()
    ma20 = close.rolling(MA_MID).mean()
    ma_turn_up = ma5.iloc[-1] > ma5.iloc[-2]                                     # 5日均线拐头
    uptrend = (close.iloc[-1] > ma20.iloc[-1]) & (ma5.iloc[-1] > ma20.iloc[-1])  # 上升趋势
    trend_ok = ma_turn_up | uptrend

    valid = surge & trend_ok
    if not valid.any():
        return []

    # ---- 3) 板块：最近10日涨幅排名前10的申万一级行业 ----
    ret10 = close.iloc[-1] / close.iloc[-11] - 1
    ret5 = close.iloc[-1] / close.iloc[-6] - 1
    money5 = money.iloc[-5:].mean()

    ind_map = industry_map(context, universe)
    ind_ret = {}
    for code in universe:
        ind = ind_map.get(code)
        if ind is None or code not in ret10.index:
            continue
        if pd.isna(ret10[code]):
            continue
        ind_ret.setdefault(ind, []).append(ret10[code])
    ind_mean = {k: float(np.mean(v)) for k, v in ind_ret.items() if v}
    top_ind = set(sorted(ind_mean, key=ind_mean.get, reverse=True)[:SECTOR_TOP_N])

    filtered = [c for c in valid.index if valid[c] and ind_map.get(c) in top_ind]
    if not filtered:
        return []

    # ---- 4) 热度排序：换手率优先，成交额兜底 ----
    tr = turnover_heat(context, filtered)

    def heat(code):
        v = tr.get(code)
        if v is not None and not pd.isna(v):
            return float(v)
        return float(money5[code]) / 1e8  # 兜底：近5日平均成交额（亿元）

    filtered.sort(key=heat, reverse=True)
    return filtered


def industry_map(context, universe):
    """返回 {股票代码: 申万一级行业代码}。"""
    try:
        info = get_industry(universe, date=context.previous_date)
    except Exception:
        return {}
    m = {}
    for code in universe:
        d = info.get(code)
        if isinstance(d, dict):
            sw = d.get('sw_l1') or {}
            ind = sw.get('industry_code')
            if ind:
                m[code] = ind
    return m


def turnover_heat(context, codes):
    """返回 {股票代码: 换手率}，作为“市场热度”的度量。"""
    if not codes:
        return {}
    try:
        q = query(valuation.code, valuation.turnover_ratio).filter(
            valuation.code.in_(codes))
        df = get_fundamentals(q, date=context.previous_date)
    except Exception:
        return {}
    if df is None or len(df) == 0:
        return {}
    return dict(zip(df['code'].tolist(), df['turnover_ratio'].tolist()))


# ------------------------------ 下单 ------------------------------

def order_target_value_with_tolerance(context, security, target):
    """目标价值下单，带 1% 容差，避免每日微调产生不必要的交易。"""
    pos = context.portfolio.positions.get(security)
    cur = pos.value if pos is not None else 0.0
    if context.portfolio.total_value > 0 and \
            abs(cur - target) / context.portfolio.total_value < 0.01:
        return
    order_target_value(security, target)

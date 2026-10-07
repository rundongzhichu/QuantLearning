# -*- coding: utf-8 -*-
"""
热度 + 放量 + 均线选股策略（沪深A股，参照沪深300指数择时）。

【选股条件】（全部满足，才能进入候选池）
1. 放量：最近5日平均成交量 >= 之前5日平均成交量的 2 倍（即增加100%）
2. 形态：5日均线拐头向上，或处于上升趋势（收盘价 > MA20 且 MA5 > MA20）
3. 板块：所属申万一级行业最近10日涨幅排名前10
4. 热度：在满足上述条件的股票里按“热度”排序，取前3只作为候选
   （热度优先用换手率，取不到时退化为近5日平均成交额）

【择时】（参照沪深300指数）
- 下跌趋势 或 缩量      → 空仓（清掉所有持仓）
- 上涨趋势 / 放量 / 震荡 → 开仓，总仓位上限 60%
- 沪深300 当日涨幅 > 1.5% → 仓位降到 30%

【仓位管理】
- 单只股票 <= 20%，总仓位 <= 60%（即最多 3 只）
- 只要个股还在合格池（通过全部选股条件）就继续持有，只在跌破条件时卖出
- 持仓 < 3 只：按热度从高到低买入候选补齐
- 指数择时信号变化时（如涨超1.5%降到30%）对持仓下调目标仓位

【盘中交易（分钟级；T+1 下只能卖可卖部分）】
- 加仓：标的反弹（站上均价线且高于开盘）且放量（当日累计量 > 按时间推算的基准量*1.5）→ 加仓，单只上限 20%
- 卖出：当日为正收益，且较当日最高价回落 >= 3% → 卖出可卖部分，保住当日收益

说明：本文件自包含，不依赖 JoinQuant.common 等公共模块。
选股/择时为日线；盘中加仓与保收益为分钟级，回测请选“分钟”频率才能生效。
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
MAX_EXPOSURE = 0.60      # 总仓位上限 60%
REDUCED_EXPOSURE = 0.30  # 指数大涨时降到的仓位 30%
INDEX_SURGE = 0.015      # 沪深300 单日涨幅阈值 1.5%
SECTOR_TOP_N = 10        # 板块涨幅排名前 N
SECTOR_DAYS = 10         # 板块涨幅统计窗口
INDEX_CODE = '000300.XSHG'

# 盘中交易参数
MAX_SINGLE = 0.20        # 单只股票仓位上限 20%
BASE_WEIGHT = 0.10       # 开盘建立的基础仓位（每只，盘中再视信号加仓）
ADD_STEP = 0.05          # 每次盘中加仓幅度
INTRA_ADD_VOL = 1.5      # 盘中“反弹放量”的量比阈值
PULLBACK = 0.03          # 高点回落触发卖出阈值 3%


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

    g.candidates = []     # 今日候选（合格池里热度前3，从高到低）
    g.qualified = set()   # 今日合格池（通过全部选股条件的股票）
    g.exposure = 0.0      # 今日目标总仓位
    g.target_holdings = []  # 今日目标持仓（开盘建仓 + 盘中加仓的标的）
    g.base_volume = {}    # 标的基础日成交量（近5日均量，用于盘中量比）
    g.intraday_high = {}  # 当日最高价（盘中跟踪）
    g.sold_today = set()  # 当日已触发高点回落卖出的股票
    g.cum_volume = {}     # 当日累计成交量（盘中逐bar累加）

    run_daily(before_trading, time='before_open', reference_security=INDEX_CODE)
    run_daily(market_open, time='open', reference_security=INDEX_CODE)
    run_daily(after_trading, time='after_close', reference_security=INDEX_CODE)


def before_trading(context):
    """开盘前：计算指数择时信号、合格池、候选池与盘中量比基准。"""
    g.exposure = index_exposure(context)
    g.candidates = []
    g.qualified = set()
    g.target_holdings = []
    g.base_volume = {}
    g.intraday_high = {}
    g.sold_today = set()
    g.cum_volume = {}

    if g.exposure <= 0:
        log.info('指数空仓信号（下跌趋势或缩量）')
        return
    universe = get_a_share_universe(context)
    qualified = pick_candidates(context, universe)   # 全部合格股票（按热度排序）
    g.qualified = set(qualified)
    g.candidates = qualified[:CANDIDATE_N]

    # 盘中量比基准：候选 + 当前持仓的近5日均量
    tracked = set(g.candidates) | set(context.portfolio.positions.keys())
    for s in tracked:
        df = attribute_history(s, 6, '1d', ['volume'], skip_paused=True, df=True)
        if df is not None and len(df) >= 5:
            g.base_volume[s] = float(df['volume'].iloc[-5:].mean())

    log.info('合格 %d 只，候选 %d 只：%s' %
             (len(g.qualified), len(g.candidates), ','.join(g.candidates)))


def market_open(context):
    """开盘时：跌破条件卖出；已有持仓只降不升；新进标的建基础仓（盘中再加）。"""
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

    # 1) 只在跌破选股条件（不在合格池）时卖出
    for s in holding:
        if s not in qualified:
            order_target_value(s, 0)
            log.info('卖出（跌破选股条件） %s' % s)

    # 2) 确定目标持仓：现有合格持仓 + 热度最高的候选补齐，最多3只
    keep = [s for s in holding if s in qualified]
    if len(keep) > MAX_POSITIONS:                 # 正常不会发生，仅兜底
        rank = {c: i for i, c in enumerate(candidates)}
        keep.sort(key=lambda s: rank.get(s, 999))
        keep = keep[:MAX_POSITIONS]
    for s in candidates:
        if len(keep) >= MAX_POSITIONS:
            break
        if s not in keep:
            keep.append(s)

    g.target_holdings = keep
    if not keep:
        return

    # 3) 开盘仓位：单只上限 = min(20%, 总仓位/3)；新进只建基础仓，盘中再视信号加仓
    single_target = min(MAX_SINGLE, g.exposure / MAX_POSITIONS)
    for s in keep:
        pos = positions.get(s)
        cur_ratio = pos.value / total if pos is not None else 0.0
        if pos is not None and cur_ratio > 0:
            # 已有持仓：只降不升（指数降仓位时下调；加仓交给盘中信号）
            if cur_ratio > single_target:
                order_target_value_with_tolerance(context, s, total * single_target)
        else:
            # 新进标的：先建基础仓
            order_target_value_with_tolerance(context, s, total * BASE_WEIGHT)

    log.info('目标持仓 %s，单只上限 %.0f%%，新进基础仓 %.0f%%'
             % (','.join(keep), single_target * 100, BASE_WEIGHT * 100))


def handle_data(context, data):
    """盘中（每个bar）：反弹放量加仓；高点回落卖出保住当日收益。

    用 data[股票] 的 bar 数据（close/high/low/volume/avg），比 get_current_data 字段更全。
    """
    if g.exposure <= 0:
        return
    total = context.portfolio.total_value
    if total <= 0:
        return
    positions = context.portfolio.positions
    now = context.current_dt.time()
    can_buy = now.hour * 60 + now.minute <= 14 * 60 + 45   # 尾盘不再加仓

    single_target = min(MAX_SINGLE, g.exposure / MAX_POSITIONS)
    current = get_current_data()

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
        day_open = cd.day_open or 0.0
        if last <= 0:
            continue

        # 累计当日成交量（data 的 volume 是当前 bar 成交量，逐 bar 累加）
        g.cum_volume[s] = g.cum_volume.get(s, 0.0) + float(getattr(bar, 'volume', 0.0) or 0.0)

        # 更新当日最高价（滚动跟踪）
        b_high = getattr(bar, 'high', 0.0) or last
        high = max(g.intraday_high.get(s, b_high), b_high)
        g.intraday_high[s] = high

        day_ret = last / day_open - 1 if day_open > 0 else 0.0

        # ---- 高点回落卖出（保住当日收益）----
        # 当日为正收益，且较当日最高价回落超过阈值 → 卖出可卖部分
        if s not in g.sold_today and high > 0 and day_ret > 0 \
                and (high - last) / high >= PULLBACK:
            closeable = pos.closeable_amount
            if closeable > 0:
                order(s, -closeable)
                log.info('高点回落卖出（保收益） %s，高点 %.2f 现价 %.2f' % (s, high, last))
                g.sold_today.add(s)
            continue

        # ---- 反弹放量加仓（尾盘不再加仓）----
        if not can_buy or s not in g.target_holdings:
            continue
        avg = getattr(bar, 'avg', 0.0) or 0.0
        rebound = (last > avg and last > day_open) if avg > 0 else (last > day_open)
        if not rebound:
            continue
        vol_ratio = intraday_volume_ratio(context, s, g.cum_volume.get(s, 0.0))
        if vol_ratio < INTRA_ADD_VOL:
            continue
        cur_ratio = pos.value / total
        if cur_ratio >= single_target - 0.005:
            continue
        total_ratio = sum(p.value for p in positions.values()) / total
        if total_ratio >= g.exposure - 0.005:
            continue
        new_ratio = min(single_target, cur_ratio + ADD_STEP)
        order_target_value_with_tolerance(context, s, total * new_ratio)
        log.info('反弹放量加仓 %s，现价 %.2f 量比 %.2f → 仓位 %.0f%%'
                 % (s, last, vol_ratio, new_ratio * 100))


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


def intraday_volume_ratio(context, security, cur_volume):
    """当日累计成交量 / 按时间推算的基准量（近5日均量 * 已过时间比例）。"""
    base = g.base_volume.get(security)
    if not base or base <= 0:
        return 0.0
    minutes = elapsed_minutes(context.current_dt.time())
    if minutes < 10:                      # 开盘前几分钟量比无意义
        return 0.0
    expected = base * minutes / 240.0     # 沪深每天240分钟
    if expected <= 0:
        return 0.0
    return float(cur_volume) / expected


def elapsed_minutes(t):
    """距 9:30 开盘已交易的分钟数（扣除午间休市）。"""
    m = t.hour * 60 + t.minute
    am_start, am_end = 9 * 60 + 30, 11 * 60 + 30
    pm_start, pm_end = 13 * 60, 15 * 60
    if m <= am_start:
        return 0
    if m <= am_end:
        return m - am_start
    if m <= pm_start:
        return am_end - am_start
    if m <= pm_end:
        return (am_end - am_start) + (m - pm_start)
    return 240

# -*- coding: utf-8 -*-
"""
放量后横盘选股策略（沪深A股，参照沪深300指数择时）—— 移动止盈版。

============================================================================
单文件框架：择时 → 仓位规划 → 选股 → 买卖时机 → 止盈止损
============================================================================

【1 择时】（参照沪深300，决定“今天能不能开仓、能开多少仓”）
  - 下跌趋势（MA5<MA20<MA60）或 缩量 → 空仓（目标仓位 0）
  - 上涨 / 放量 / 震荡 → 开仓，目标总仓位 60%
  - 指数当日涨幅 > 1.5% → 目标仓位降到 30%
  - 附带节假日日历：连续休市 >= 3 天时，节前倒数第 2 天清仓、节前最后 1 天尾盘买入

【2 仓位规划】（决定“最多持几只、每只买多少”）
  - 单只 <= 20%，总仓位 <= 60%（正常最多 3 只）
  - 名额分配：被迫持有 + 合格持仓 + 按评分补齐，不超可持仓数
  - 买不起一手 → 用下一个候选补位
  - 空仓/降仓状态：止损级联降仓、连续盈利/亏损后的空仓与阶梯回补

【3 选股】（决定“买哪几只”）
  - 股票池：沪深A股（剔除ST/退市、上市不足60日、停牌）
  - 放量：过去 SURGE_WINDOW 日内出现单日成交量 >= 之前 SURGE_BASE_DAYS 日均量的 VOL_SURGE 倍
  - 横盘：放量日之后至少 FLAT_MIN_DAYS 个交易日，最高/最低收盘价振幅 <= FLAT_RANGE
  - 排序：横盘振幅越小、放量倍数越大、近端成交越活跃者优先，取前 CANDIDATE_N 只

【4 买卖时机】（决定“什么时候下单”）
  - 盘前 before_open：择时 + 状态刷新
  - 开盘 open：空仓/节假日清仓
  - 10:30：选股 + 名额分配 + 开仓
  - 14:30：节前最后 1 个交易日买入过节仓位
  - 盘中 handle_data：逐 bar 跑止损/止盈

【5 止盈止损】（决定“什么时候离场”）
  - 严格止损：盘中每 bar 监控，当日涨幅跌破 MIN_GAIN 即卖（每只至少持有 MIN_HOLD_DAYS 天）
  - 移动止盈：相对成本涨 > TAKE_PROFIT_ARM 后，从动态峰值回落 TRAILING_PULLBACK 立即卖
  - 级联降仓：止损触发后 3→2→1→0，各档维持 1/2/4 个交易日
  - 冷却：移动止盈后 TP_COOLDOWN_DAYS 天内禁止买入

说明：本文件自包含，不依赖 JoinQuant.common 等公共模块。
选股使用上一交易日及之前的日线数据；止损/止盈为分钟级，回测请选“分钟”频率；日线回测下盘中逻辑会退化为每天一次。
"""
from jqdata import *
from datetime import timedelta


# ============================================================================
# 0. 全局状态与参数
# ============================================================================

# ---- 择时参数 ----
INDEX_CODE = '000300.XSHG'
MA_SHORT = 5             # 短期均线
MA_MID = 20              # 中期均线
MA_LONG = 60             # 长期均线
MAX_EXPOSURE = 0.60      # 正常目标总仓位 60%
REDUCED_EXPOSURE = 0.30  # 指数大涨时降到的仓位 30%
INDEX_SURGE = 0.015      # 沪深300 单日涨幅阈值 1.5%

# ---- 选股参数 ----
CANDIDATE_N = 3          # 候选股票数量
SURGE_WINDOW = 10        # 放量日回看窗口（过去 N 个交易日内出现放量）
SURGE_BASE_DAYS = 20     # 放量基准：放量日之前 N 日均量
VOL_SURGE = 2.0          # 放量倍数：单日量 >= 前 SURGE_BASE_DAYS 日均量 * 2
FLAT_MIN_DAYS = 3        # 放量后横盘的最少交易日数
FLAT_RANGE = 0.08        # 横盘振幅上限：最高/最低收盘价之差 <= 8%
HIST_DAYS = 40           # 每只股票拉取的日线根数（需覆盖放量+横盘+基准）

# ---- 仓位规划参数 ----
MAX_POSITIONS = 3        # 最大持仓数量
MAX_SINGLE = 0.20        # 单只股票仓位上限 20%
MIN_HOLD_DAYS = 3        # 每只股票至少持有的交易日数

# ---- 止盈止损参数 ----
TAKE_PROFIT_ARM = 0.05    # 以成本价为基准，盈利 > 5% 后启动移动止盈
TRAILING_PULLBACK = 0.02  # 从动态峰值回落 2% 立即止盈
TP_COOLDOWN_DAYS = 3      # 移动止盈后 N 天内禁止买入
MIN_GAIN = 0.01           # 盘中严格止损：当日涨幅跌破该下限即卖出
PENALTY_DAYS = [1, 2, 4]  # 止损级联降仓：各档维持的交易日数（3→2→1→0）
WIN_STREAK = 2            # 连续盈利 N 个交易日触发空仓
WIN_COOL_DAYS = 3         # 连续盈利后空仓的交易日数
LOSS_STREAK = 2           # 连续亏损 N 个交易日触发空仓


# ============================================================================
# 6. 回调入口（JoinQuant 事件钩子）
# ============================================================================

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
    reset_state()

    run_daily(before_trading, time='before_open', reference_security=INDEX_CODE)
    run_daily(market_open, time='open', reference_security=INDEX_CODE)
    run_daily(open_positions, time='10:30', reference_security=INDEX_CODE)
    run_daily(buy_holiday_positions, time='14:30', reference_security=INDEX_CODE)
    run_daily(after_trading, time='after_close', reference_security=INDEX_CODE)


def reset_state():
    """跨日复用的全局状态。"""
    g.candidates = []       # 今日候选（放量后横盘，按评分排序）
    g.qualified = set()     # 今日合格池（通过全部选股条件的股票）
    g.pre_close = {}        # 昨收价缓存 {股票: 昨收}，供盘中严格止损使用
    g.exposure = 0.0        # 择时输出：今日目标总仓位
    g.target_holdings = []  # 今日目标持仓
    g.peak_price = {}       # 持仓动态峰值（移动止盈用，跨日保持）
    g.tp_date = {}          # 移动止盈卖出日期 → 冷却期内禁止买入
    g.penalty_level = 0     # 止损级联档位 0/1/2/3 → 可持仓 3/2/1/0
    g.penalty_left = 0      # 当前档位剩余维持交易日数
    g.max_positions = MAX_POSITIONS   # 级联降仓后的可持仓上限
    g.entry_date = {}       # 建仓日 {股票: 日期}，用于最少持有天数
    g.holiday_state = None  # 节假日状态：None / 'clear_day' / 'buy_day'
    g.profit_streak = 0     # 连续盈利交易日计数
    g.prev_close_value = None   # 上一交易日收盘总资产，用于判定当日是否盈利
    g.win_cool_until = None     # 连续盈利空仓截止日期（含当天），None 表示未触发
    g.loss_streak = 0           # 连续亏损交易日计数
    g.recovery_limit = None     # 连续亏损后的阶梯回补档位：0/1/2/3，None 为正常


def before_trading(context):
    """盘前：择时 + 状态刷新。"""
    g.exposure = compute_exposure(context)               # 1 择时
    g.candidates = []
    g.qualified = set()
    g.target_holdings = []
    g.holiday_state = compute_holiday_state(context)     # 1 择时·节假日
    update_risk_state(context)                           # 2 仓位规划·状态递减

    if g.exposure <= 0:
        log.info('指数空仓信号（下跌趋势或缩量）')


def market_open(context):
    """开盘：空仓/节假日清仓。"""
    positions = context.portfolio.positions
    holding = [s for s in positions if positions[s].total_amount > 0]

    reason = empty_reason(context)                       # 2 仓位规划·空仓判断
    if reason is not None:
        _liquidate(holding, reason)
        g.target_holdings = []
        return

    if g.holiday_state == 'clear_day':                   # 4 买卖时机·节前清仓
        _liquidate(holding, '节前倒数第2个交易日清仓')
        g.target_holdings = []


def open_positions(context):
    """10:30：选股 → 名额分配 → 下单开仓。"""
    if empty_reason(context) is not None:
        return
    if g.holiday_state == 'clear_day':
        return   # 节前倒数第2天，全天空仓

    universe = build_universe(context)
    candidates = pick_candidates(context, universe)      # 3 选股
    g.qualified = set(candidates)
    g.candidates = candidates
    log.info('候选 %d 只：%s' % (len(g.candidates), ','.join(g.candidates)))

    positions = context.portfolio.positions
    holding = [s for s in positions if positions[s].total_amount > 0]

    if g.holiday_state == 'buy_day':                     # 4 买卖时机·节前最后1天
        g.target_holdings = []
        log.info('节前最后交易日，14:30 后买入过节仓位')
        return

    max_pos = effective_max_positions()                  # 2 仓位规划
    target = plan_positions(context, holding, g.qualified, candidates, max_pos)
    g.target_holdings = target
    if not target:
        return

    execute_orders(context, target, candidates, holding, max_pos)

    log.info('目标持仓 %s，可持仓上限 %d，单只上限 %.0f%%'
             % (','.join(target), max_pos, single_target_value(g.exposure, max_pos) * 100))


def handle_data(context, data):
    """盘中每 bar：止损 + 止盈。"""
    intraday_stop_loss(context, data)                    # 5 止盈止损·严格止损
    trailing_take_profit(context, data)                  # 5 止盈止损·移动止盈


def after_trading(context):
    """盘后：观测日志 + 连盈/连亏统计。"""
    positions = context.portfolio.positions
    total = context.portfolio.total_value
    weight = sum(p.value for p in positions.values()) / total if total > 0 else 0.0
    log.info('持仓 %d 只，实际总仓位 %.1f%%' % (len(positions), weight * 100))
    evaluate_daily_pnl(context)                          # 2 仓位规划·空仓策略统计


# ============================================================================
# 1. 择时（指数趋势 / 量能 / 节假日）
# ============================================================================

def compute_exposure(context):
    """参照沪深300，返回今日目标总仓位（0 / 30% / 60%）。"""
    df = attribute_history(INDEX_CODE, MA_LONG + 5, '1d',
                           ['close', 'volume'], skip_paused=True, df=True)
    if df is None or len(df) < MA_LONG + 1:
        return 0.0

    close = df['close']
    vol = df['volume']

    ma5 = close.rolling(MA_SHORT).mean()
    ma20 = close.rolling(MA_MID).mean()
    ma60 = close.rolling(MA_LONG).mean()

    trend_down = (ma5.iloc[-1] < ma20.iloc[-1]) and (ma20.iloc[-1] < ma60.iloc[-1])

    vol5 = vol.iloc[-5:].mean()
    vol_prev5 = vol.iloc[-10:-5].mean()
    vol_shrinking = vol_prev5 > 0 and vol5 < vol_prev5      # 缩量
    vol_expanding = vol_prev5 > 0 and vol5 > vol_prev5      # 放量

    day_ret = close.iloc[-1] / close.iloc[-2] - 1

    if trend_down or vol_shrinking:
        log.info('指数：下跌趋势=%s 缩量=%s → 空仓' % (trend_down, vol_shrinking))
        return 0.0
    if day_ret > INDEX_SURGE:
        log.info('指数当日涨幅 %.2f%% > 1.5%%，仓位降至 30%%' % (day_ret * 100))
        return REDUCED_EXPOSURE
    log.info('指数：放量=%s 涨幅 %.2f%% → 开仓' % (vol_expanding, day_ret * 100))
    return MAX_EXPOSURE


def compute_holiday_state(context):
    """节假日日历：返回 None / 'clear_day'（节前倒数第2天）/ 'buy_day'（节前最后1天）。"""
    today = context.current_dt.date()
    try:
        raw = get_trade_days(start_date=today - timedelta(days=30),
                             end_date=today + timedelta(days=30))
        days = [d.date() if hasattr(d, 'date') else d for d in raw]
    except Exception:
        return None
    if today not in days:
        return None

    i = days.index(today)
    for j in range(i, len(days) - 1):
        if (days[j + 1] - days[j]).days >= 4:      # 下一个连续休市 >=3 天的节假日
            if j == i:
                return 'buy_day'
            if j == i + 1:
                return 'clear_day'
            return None
    return None


# ============================================================================
# 2. 仓位规划（名额分配 / 空仓状态 / 单只目标）
# ============================================================================

def update_risk_state(context):
    """盘前：清理止盈冷却、级联惩罚递减、恢复可持仓数。"""
    today = context.current_dt.date()
    if g.tp_date:
        g.tp_date = {s: d for s, d in g.tp_date.items()
                     if (today - d).days <= TP_COOLDOWN_DAYS}

    if g.penalty_left > 0:
        g.penalty_left -= 1
        if g.penalty_left == 0:
            g.penalty_level = 0
    g.max_positions = MAX_POSITIONS - g.penalty_level


def empty_reason(context):
    """空仓判断：返回空仓原因字符串；不空仓返回 None。"""
    today = context.current_dt.date()
    if g.exposure <= 0:
        return '指数空仓'
    if g.win_cool_until is not None and today <= g.win_cool_until:
        return '连续盈利空仓'
    if g.recovery_limit == 0:
        return '连续亏损空仓'
    if g.max_positions <= 0:
        return '级联惩罚空仓'
    return None


def effective_max_positions():
    """有效可持仓数 = min(级联降仓上限, 连亏阶梯回补档位)。"""
    mp = g.max_positions
    if g.recovery_limit is not None:
        mp = min(mp, g.recovery_limit)
    return mp


def single_target_value(exposure, max_pos):
    """单只目标仓位 = min(单只上限 20%, 总仓位 / 可持仓数)。"""
    return min(MAX_SINGLE, exposure / max_pos)


def evaluate_daily_pnl(context):
    """盘后：统计连盈/连亏，触发空仓与阶梯回补。"""
    total = context.portfolio.total_value
    today = context.current_dt.date()

    profitable_today = g.prev_close_value is not None and total > g.prev_close_value
    losing_today = g.prev_close_value is not None and total < g.prev_close_value
    if profitable_today:
        g.loss_streak = 0
        if g.recovery_limit is None:
            g.profit_streak += 1      # 回补期间盈利不累计连胜
    elif losing_today:
        g.loss_streak += 1
        g.profit_streak = 0
    else:
        g.profit_streak = 0
        g.loss_streak = 0
    g.prev_close_value = total

    if g.profit_streak >= WIN_STREAK:
        g.profit_streak = 0
        try:
            days = get_trade_days(start_date=today, count=WIN_COOL_DAYS + 1)
            last = days[-1]
            g.win_cool_until = last.date() if hasattr(last, 'date') else last
        except Exception:
            g.win_cool_until = today + timedelta(days=WIN_COOL_DAYS)
        log.info('连续盈利 %d 天，触发空仓 %d 天' % (WIN_STREAK, WIN_COOL_DAYS))

    if g.loss_streak >= LOSS_STREAK:
        g.loss_streak = 0
        g.recovery_limit = 0          # 先空仓 1 个交易日
        log.info('连续亏损 %d 天，触发空仓 1 天后阶梯回补' % LOSS_STREAK)
    elif g.recovery_limit is not None:
        if g.recovery_limit == 0:
            g.recovery_limit = 1      # 空仓 1 天后，下一交易日开 1 只
        elif profitable_today:
            g.recovery_limit += 1     # 当日盈利才加一只
        if g.recovery_limit >= MAX_POSITIONS:
            g.recovery_limit = None   # 回到正常 3 只


def plan_positions(context, holding, qualified, candidates, max_pos):
    """名额分配：被迫持有 + 合格持仓 + 按评分补齐，返回目标持仓列表。"""
    today = context.current_dt.date()

    forced = [s for s in holding
              if s not in qualified
              and g.entry_date.get(s) is not None
              and held_trading_days(context, g.entry_date[s]) < MIN_HOLD_DAYS]
    keep = [s for s in holding if s in qualified]

    room = max_pos - len(forced)
    if room < 0:
        room = 0
    if len(keep) > room:                       # 名额不足：按评分砍掉靠后的合格持仓
        rank = {c: i for i, c in enumerate(candidates)}
        keep.sort(key=lambda s: rank.get(s, 999))
        keep = keep[:room]
    for s in candidates:
        if len(keep) >= room:
            break
        if s in keep or s in forced:
            continue
        if s in g.tp_date and (today - g.tp_date[s]).days <= TP_COOLDOWN_DAYS:
            continue   # 移动止盈冷却期内不重新买入
        keep.append(s)

    target = forced + keep

    for s in holding:
        if s in qualified and s not in keep:
            order_target_value(s, 0)
            g.peak_price.pop(s, None)
            g.entry_date.pop(s, None)
            log.info('超出可持仓名额卖出 %s' % s)

    return target


# ============================================================================
# 3. 选股（股票池 / 放量后横盘）
# ============================================================================

def build_universe(context):
    """股票池：沪深A股（排除ST/退市、上市不足60日、已退市、停牌）。"""
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
    """放量后横盘选股：近 SURGE_WINDOW 日内出现放量，且放量后横盘整理。

    放量：单日成交量 >= 前 SURGE_BASE_DAYS 日均量 * VOL_SURGE。
    横盘：放量日之后（含最新一日）至少 FLAT_MIN_DAYS 个交易日，最高/最低收盘价振幅 <= FLAT_RANGE。
    排序：横盘振幅越小、放量倍数越大、近端成交越活跃者优先，返回前 CANDIDATE_N 只。
    """
    if not universe:
        log.info('[选股] 股票池为空')
        return []
    prev = context.previous_date

    df = get_price(universe, end_date=prev, count=HIST_DAYS,
                   frequency='daily', fields=['close', 'volume'],
                   panel=False, skip_paused=True)
    if df is None or len(df) == 0:
        log.info('[选股] 日线数据为空（universe %d 只）' % len(universe))
        return []
    df = df.reset_index()
    if 'code' not in df.columns or 'close' not in df.columns or 'volume' not in df.columns:
        log.info('[选股] 日线数据列异常：%s' % list(df.columns))
        return []

    g.pre_close.update(dict(zip(df['code'], df['close'])))  # 供盘中严格止损使用

    scored = []
    for code, sub in df.groupby('code'):
        closes = sub['close'].astype(float).values
        vols = sub['volume'].astype(float).values
        n = len(closes)
        if n < SURGE_BASE_DAYS + FLAT_MIN_DAYS + 1:
            continue

        # 近 SURGE_WINDOW 日内找放量日（需留出其后至少 FLAT_MIN_DAYS 天做横盘）
        surge_idx = -1
        surge_ratio = 0.0
        for i in range(max(0, n - SURGE_WINDOW), n - FLAT_MIN_DAYS):
            base = vols[max(0, i - SURGE_BASE_DAYS):i]
            if len(base) == 0 or base.mean() <= 0:
                continue
            ratio = vols[i] / base.mean()
            if ratio >= VOL_SURGE and ratio > surge_ratio:
                surge_idx = i
                surge_ratio = ratio

        if surge_idx < 0:
            continue

        # 放量日之后（含最新）连续 FLAT_MIN_DAYS 个交易日收盘价振幅足够小
        flat = closes[surge_idx + 1:surge_idx + 1 + FLAT_MIN_DAYS]
        if len(flat) < FLAT_MIN_DAYS:
            continue
        hi = flat.max()
        lo = flat.min()
        if hi <= 0 or lo <= 0:
            continue
        flat_range = (hi - lo) / lo
        if flat_range > FLAT_RANGE:
            continue

        recent_vol = vols[-FLAT_MIN_DAYS:].sum()
        scored.append((flat_range, -surge_ratio, -recent_vol, code))

    scored.sort()
    candidates = [item[3] for item in scored[:CANDIDATE_N]]
    log.info('[选股] 放量后横盘合格 %d 只，候选 %d 只' % (len(scored), len(candidates)))
    return candidates


# ============================================================================
# 4. 买卖时机（下单执行 / 节前买卖 / 补位）
# ============================================================================

def execute_orders(context, target, candidates, holding, max_pos):
    """下单：新进直接建满；买不起一手时用下一个候选补位。"""
    total = context.portfolio.total_value
    positions = context.portfolio.positions
    today = context.current_dt.date()
    single_target = single_target_value(g.exposure, max_pos)

    held_set = set(holding)
    extra = [s for s in candidates if s not in target and s not in held_set]
    extra_i = 0

    for s in target:
        pos = positions.get(s)
        cur_ratio = pos.value / total if pos is not None else 0.0
        if pos is not None and cur_ratio > 0:
            if cur_ratio > single_target:
                order_target_value_with_tolerance(context, s, total * single_target)
            continue
        if order_target_value_with_tolerance(context, s, total * single_target):
            g.entry_date[s] = today
            continue
        # 买不起一手：从剩余候选里补位
        while extra_i < len(extra):
            sub = extra[extra_i]
            extra_i += 1
            if sub in g.tp_date and (today - g.tp_date[sub]).days <= TP_COOLDOWN_DAYS:
                continue   # 移动止盈冷却期内不补
            if order_target_value_with_tolerance(context, sub, total * single_target):
                g.entry_date[sub] = today
                g.target_holdings.append(sub)
                break


def buy_holiday_positions(context):
    """节前买入：节前最后 1 个交易日 14:30 后，按候选顺序买入过节仓位。"""
    if g.holiday_state != 'buy_day':
        return
    if g.exposure <= 0 or g.max_positions <= 0:
        return

    total = context.portfolio.total_value
    positions = context.portfolio.positions
    held = [s for s in positions if positions[s].total_amount > 0]
    candidates = g.candidates
    today = context.current_dt.date()
    max_pos = g.max_positions
    single_target = single_target_value(g.exposure, max_pos)

    for s in candidates:
        if len(held) >= max_pos:
            break
        if s in held:
            continue
        if s in g.tp_date and (today - g.tp_date[s]).days <= TP_COOLDOWN_DAYS:
            continue
        if order_target_value_with_tolerance(context, s, total * single_target):
            g.entry_date[s] = today
            held.append(s)

    g.target_holdings = held
    if held:
        log.info('节前最后交易日 14:30 后建仓 %s' % ','.join(held))


# ============================================================================
# 5. 止盈止损（严格止损 / 移动止盈 / 级联降仓）
# ============================================================================

def intraday_stop_loss(context, data):
    """严格止损（盘中每 bar）：当日涨幅跌破 MIN_GAIN 即全部卖出；持有满 MIN_HOLD_DAYS 天才触发。"""
    positions = context.portfolio.positions
    holding = [s for s in positions if positions[s].total_amount > 0]
    if not holding:
        return
    current = get_current_data()

    for s in holding:
        entry = g.entry_date.get(s)
        if entry is not None and held_trading_days(context, entry) < MIN_HOLD_DAYS:
            continue   # 持有未满 MIN_HOLD_DAYS 个交易日，暂不止损
        cd = current[s]
        if cd.paused:
            continue
        last = getattr(cd, 'last_price', 0.0) or 0.0
        if last <= 0:
            continue
        pre = g.pre_close.get(s)
        if not pre or pre <= 0:
            pre = _pre_close(s, context.previous_date)
            g.pre_close[s] = pre
        if pre <= 0:
            continue
        if last / pre - 1 >= MIN_GAIN:
            continue   # 涨幅仍在买入区间内，不止损
        closeable = positions[s].closeable_amount
        if closeable <= 0:
            continue
        order(s, -closeable)
        g.peak_price.pop(s, None)
        g.entry_date.pop(s, None)
        escalate_penalty()   # 触发一次止损 → 级联降仓
        log.info('盘中跌破 %.0f%% 止损 %s，可持仓上限 %d'
                 % (MIN_GAIN * 100, s, g.max_positions))


def trailing_take_profit(context, data):
    """移动止盈：相对成本涨 > TAKE_PROFIT_ARM 后，从动态峰值回落 TRAILING_PULLBACK 立即全部卖出。"""
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

        b_high = getattr(bar, 'high', 0.0) or last
        peak = max(g.peak_price.get(s, 0.0), b_high)
        g.peak_price[s] = peak

        cost = pos.avg_cost or 0.0
        if cost > 0 and peak > cost * (1 + TAKE_PROFIT_ARM)                 and last <= peak * (1 - TRAILING_PULLBACK):
            closeable = pos.closeable_amount
            if closeable > 0:
                order(s, -closeable)
                log.info('移动止盈立即卖出 %s，成本 %.2f 峰值 %.2f 现价 %.2f'
                         % (s, cost, peak, last))
                g.peak_price.pop(s, None)
                g.entry_date.pop(s, None)
                g.tp_date[s] = today   # 记录止盈日期，冷却期内禁止买入


def escalate_penalty():
    """止损级联：可持仓数量级联下调（3→2→1→0），维持天数指数递增（1→2→4）。"""
    if g.penalty_level == 0:
        g.penalty_level = 1
        g.penalty_left = PENALTY_DAYS[0]
    elif g.penalty_level == 1:
        g.penalty_level = 2
        g.penalty_left = PENALTY_DAYS[1]
    elif g.penalty_level == 2:
        g.penalty_level = 3
        g.penalty_left = PENALTY_DAYS[2]
    g.max_positions = MAX_POSITIONS - g.penalty_level


# ============================================================================
# 7. 工具函数
# ============================================================================

def _liquidate(holding, reason):
    """清空给定持仓，并清理峰值/建仓日状态。"""
    for s in holding:
        order_target_value(s, 0)
        g.peak_price.pop(s, None)
        g.entry_date.pop(s, None)
    if holding:
        log.info('%s，清仓 %s' % (reason, ','.join(holding)))


def order_target_value_with_tolerance(context, security, target):
    """目标价值下单，带 1% 容差；开仓金额不足一手时跳过。返回是否已发起下单。"""
    pos = context.portfolio.positions.get(security)
    cur = pos.value if pos is not None else 0.0
    if context.portfolio.total_value > 0 and \
            abs(cur - target) / context.portfolio.total_value < 0.01:
        return False

    holding_amount = pos.total_amount if pos is not None else 0
    if holding_amount == 0 and target > 0:
        price = _last_price(security)
        if price > 0 and target < price * 100:
            log.info('目标金额 %.0f 不足一手（%s 现价 %.2f），跳过建仓'
                     % (target, security, price))
            return False

    order_target_value(security, target)
    return True


def _last_price(security):
    """当前价：优先实时 last_price，回退到上一交易日收盘价。"""
    try:
        p = getattr(get_current_data()[security], 'last_price', 0.0) or 0.0
        if p > 0:
            return float(p)
    except Exception:
        pass
    try:
        h = attribute_history(security, 1, '1d', ['close'], df=False)
        if h and len(h.get('close', [])) > 0:
            return float(h['close'][0])
    except Exception:
        pass
    return 0.0


def _pre_close(security, prev_date):
    """昨收价（上一交易日收盘），失败返回 0。"""
    try:
        df = get_price(security, end_date=prev_date, count=1,
                       fields=['close'], panel=False, skip_paused=True)
        if df is not None and len(df) > 0:
            df = df.reset_index()
            if 'close' in df.columns:
                return float(df['close'].iloc[0])
    except Exception:
        pass
    return 0.0


def held_trading_days(context, entry_date):
    """返回从建仓日到当前交易日的交易日数（含首尾）。失败时退化为日历天数。"""
    today = context.current_dt.date()
    try:
        return len(get_trade_days(start_date=entry_date, end_date=today))
    except Exception:
        return (today - entry_date).days + 1

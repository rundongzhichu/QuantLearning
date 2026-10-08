# -*- coding: utf-8 -*-
"""
热度 + 放量 + 均线选股策略（沪深A股，参照沪深300指数择时）—— 移动止盈版。

架构：三层流水线 —— 风控 → 选股 → 仓位控制。

【第一层 风控】（先过滤：决定能不能开仓、要不要平仓）
- 择时（参照沪深300）：
  · 下跌趋势 或 缩量 → 空仓
  · 上涨 / 放量 / 震荡 → 开仓，总仓位上限 60%
  · 当日涨幅 > 1.5% → 仓位降到 30%
- 止盈止损：
  · 移动止盈：相对成本涨 > 5% 后，从动态峰值回落 2% → 立即卖出
  · 严格止损：盘中每 bar 监控，当日涨幅跌破 1% 即卖出（每只至少持有 3 个交易日才触发）
- 空仓策略：
  · 止损级联降仓：3→2→1→0，各档维持 1/2/4 个交易日
  · 连续盈利 2 个交易日 → 空仓 3 个交易日
  · 连续亏损 2 个交易日 → 空仓 1 个交易日，之后阶梯回补（1→2→3，每盈利一天加一只）
  · 节假日（连续休市 >= 3 天）：节前倒数第 2 天清仓，节前最后 1 天 14:30 后买入

【第二层 选股】（10:30 盘中实时）
- 申万二级行业按 10:30 前当日成交额排序，剔除板块涨幅非正者，取前 3 板块
- 每个板块内取当日成交额前 3 的个股（共 9 只，沪深A股含创业板）

【第三层 仓位控制】
- 单只 <= 20%，总仓位 <= 60%（正常最多 3 只）
- 名额分配：被迫持有 + 合格持仓 + 按热度补齐，不超可持仓数
- 买不起一手 → 用下一个候选补位
- 日常开仓时机：10:30（选股 + 开仓都在 10:30；空仓/清仓在开盘，严格止损盘中每 bar 监控）

【交易约束】
- 严格止损与移动止盈均在盘中监控，各自每只票至多触发一次（卖出后即清仓）
- 移动止盈后 3 天内禁止买入

说明：本文件自包含，不依赖 JoinQuant.common 等公共模块。
选股依赖 10:30 前分钟级成交额，移动止盈与节前 14:30 买入同为分钟级，回测请务必选“分钟”频率；日线回测下选股与盘中逻辑会不准确。
"""
from jqdata import *
from datetime import timedelta


# ------------------------------ 参数 ------------------------------

MA_SHORT = 5             # 短期均线
MA_MID = 20              # 中期均线
MA_LONG = 60             # 长期均线
CANDIDATE_N = 3          # 成交额前 N 的申万二级行业（板块数）
PER_SECTOR_N = 3         # 每个板块取成交额前 N 只个股
MAX_POSITIONS = 3        # 最大持仓数量
MAX_SINGLE = 0.20        # 单只股票仓位上限 20%
MAX_EXPOSURE = 0.60      # 总仓位上限 60%
REDUCED_EXPOSURE = 0.30  # 指数大涨时降到的仓位 30%
INDEX_SURGE = 0.015      # 沪深300 单日涨幅阈值 1.5%
INDEX_CODE = '000300.XSHG'

# 止盈 / 止损参数
TAKE_PROFIT_ARM = 0.05    # 以成本价为基准，盈利 > 5% 后启动移动止盈
TRAILING_PULLBACK = 0.02  # 从动态峰值回落 2% 立即止盈
TP_COOLDOWN_DAYS = 3      # 移动止盈后 N 天内禁止买入
MIN_GAIN = 0.01           # 盘中严格止损：当日涨幅跌破该下限即卖出

# 风控参数
PENALTY_DAYS = [1, 2, 4]   # 止损级联降仓：各档维持的交易日数（指数递增 1→2→4）
MIN_HOLD_DAYS = 3          # 每只股票至少持有的交易日数
WIN_STREAK = 2             # 连续盈利 N 个交易日触发空仓
WIN_COOL_DAYS = 3          # 连续盈利后空仓的交易日数
LOSS_STREAK = 2            # 连续亏损 N 个交易日触发空仓


# ================================================================
# 主回调：JoinQuant 事件入口（按「风控 → 选股 → 仓位控制」流水线调用各层）
# ================================================================

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

    g.candidates = []       # 今日候选（涨幅为正、成交额前3的申万二级行业 × 各行业成交额前3）
    g.qualified = set()     # 今日合格池（通过全部选股条件的股票）
    g.pre_close = {}        # 昨收价缓存 {股票: 昨收}，供盘中严格止损使用
    g.exposure = 0.0        # 风控·择时输出：今日目标总仓位
    g.target_holdings = []  # 今日目标持仓
    g.peak_price = {}       # 持仓动态峰值（移动止盈用，跨日保持）
    g.tp_date = {}          # 移动止盈卖出日期 → 冷却期内禁止买入
    g.penalty_level = 0     # 止损级联档位 0/1/2/3 → 可持仓 3/2/1/0
    g.penalty_left = 0      # 当前档位剩余维持交易日数
    g.max_positions = MAX_POSITIONS   # 级联降仓后的可持仓上限
    g.entry_date = {}       # 建仓日 {股票: 日期}，用于最少持有天数
    g.holiday_state = None  # 节假日状态：None / 'clear_day'（节前倒数第2天）/ 'buy_day'（节前最后1天）
    g.profit_streak = 0     # 连续盈利交易日计数
    g.prev_close_value = None   # 上一交易日收盘总资产，用于判定当日是否盈利
    g.win_cool_until = None     # 连续盈利空仓截止日期（含当天），None 表示未触发
    g.loss_streak = 0           # 连续亏损交易日计数
    g.recovery_limit = None     # 连续亏损后的阶梯回补档位：0(空仓)/1/2/3，None 为正常

    run_daily(before_trading, time='before_open', reference_security=INDEX_CODE)
    run_daily(market_open, time='open', reference_security=INDEX_CODE)
    run_daily(open_positions, time='10:30', reference_security=INDEX_CODE)
    run_daily(buy_holiday_positions, time='14:30', reference_security=INDEX_CODE)
    run_daily(after_trading, time='after_close', reference_security=INDEX_CODE)


def before_trading(context):
    """风控（择时 + 空仓状态 + 节假日）。选股在 open_positions（10:30）。"""
    g.exposure = compute_exposure(context)               # 风控·择时
    g.candidates = []
    g.qualified = set()
    g.target_holdings = []
    g.holiday_state = compute_holiday_state(context)     # 风控·节假日

    update_risk_state(context)                            # 风控·空仓状态：清理冷却、级联递减

    # 缓存当前持仓的昨收价，供盘中严格止损（涨幅跌破下限）使用
    g.pre_close = {}
    positions = context.portfolio.positions
    held = [s for s in positions if positions[s].total_amount > 0]
    if held:
        df_prev = get_price(held, end_date=context.previous_date, count=1,
                            fields=['close'], panel=False, skip_paused=True)
        if df_prev is not None and len(df_prev) > 0:
            df_prev = df_prev.reset_index()
            if 'code' in df_prev.columns and 'close' in df_prev.columns:
                g.pre_close = dict(zip(df_prev['code'], df_prev['close']))

    if g.exposure <= 0:
        log.info('指数空仓信号（下跌趋势或缩量）')


def market_open(context):
    """风控（空仓清仓）。开仓在 open_positions（10:30），止损在盘中每 bar 监控。"""
    positions = context.portfolio.positions
    holding = [s for s in positions if positions[s].total_amount > 0]

    # 风控·空仓：指数/级联/连亏/连盈 → 清仓
    reason = empty_reason(context)
    if reason is not None:
        for s in holding:
            order_target_value(s, 0)
            g.peak_price.pop(s, None)
            g.entry_date.pop(s, None)
        if holding:
            log.info('%s，清仓 %s' % (reason, ','.join(holding)))
        g.target_holdings = []
        return

    # 风控·节假日：节前倒数第2天清仓（全天保持空仓）
    if g.holiday_state == 'clear_day':
        for s in holding:
            order_target_value(s, 0)
            g.peak_price.pop(s, None)
            g.entry_date.pop(s, None)
        if holding:
            log.info('节前倒数第2个交易日清仓 %s' % ','.join(holding))
        g.target_holdings = []


def open_positions(context):
    """10:30：选股 → 名额分配 + 下单开仓（买不起一手补位）。严格止损在盘中每 bar 监控。"""
    if empty_reason(context) is not None:
        return
    if g.holiday_state == 'clear_day':
        return   # 节前倒数第2天，全天空仓

    # 选股（当日盘中数据，截至 10:30）
    universe = build_universe(context)
    candidates = pick_candidates(context, universe)
    g.qualified = set(candidates)
    g.candidates = candidates
    log.info('候选 %d 只：%s' % (len(g.candidates), ','.join(g.candidates)))

    positions = context.portfolio.positions
    holding = [s for s in positions if positions[s].total_amount > 0]

    # 节前最后1天：上午不建仓，等 14:30 买入过节仓位
    if g.holiday_state == 'buy_day':
        g.target_holdings = []
        log.info('节前最后交易日，14:30 后买入过节仓位')
        return

    max_pos = effective_max_positions()

    # 名额分配
    target = plan_positions(context, holding, g.qualified, candidates, max_pos)
    g.target_holdings = target
    if not target:
        return

    # 下单 + 补位
    execute_orders(context, target, candidates, holding, max_pos)

    log.info('目标持仓 %s，可持仓上限 %d，单只上限 %.0f%%'
             % (','.join(target), max_pos, single_target_value(g.exposure, max_pos) * 100))


def handle_data(context, data):
    """盘中每个bar：风控·严格止损（涨幅跌破下限）+ 移动止盈。"""
    intraday_stop_loss(context, data)
    trailing_take_profit(context, data)


def after_trading(context):
    """观测日志 + 风控·连盈/连亏统计。"""
    positions = context.portfolio.positions
    total = context.portfolio.total_value
    weight = sum(p.value for p in positions.values()) / total if total > 0 else 0.0
    log.info('持仓 %d 只，实际总仓位 %.1f%%' % (len(positions), weight * 100))
    evaluate_daily_pnl(context)


# ================================================================
# 第一层：风控（择时 / 空仓策略 / 止盈止损）
# ================================================================

def compute_exposure(context):
    """风控·择时：参照沪深300，返回目标总仓位（0 / 30% / 60%）。"""
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


def compute_holiday_state(context):
    """风控·节假日：返回 None / 'clear_day'（节前倒数第2天）/ 'buy_day'（节前最后1天）。

    节假日定义为连续休市 >= 3 个自然日（交易日历中相邻交易日的间隔 >= 4 天）。
    """
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
                return 'buy_day'      # 今天就是节前最后1个交易日
            if j == i + 1:
                return 'clear_day'    # 节前倒数第2个交易日
            return None
    return None


def update_risk_state(context):
    """风控·空仓状态（盘前）：清理止盈冷却、级联惩罚递减、恢复可持仓数。"""
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
    """风控·空仓判断：返回空仓原因字符串；不空仓返回 None。"""
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
    """风控·有效可持仓数 = min(级联降仓上限, 连亏阶梯回补档位)。"""
    mp = g.max_positions
    if g.recovery_limit is not None:
        mp = min(mp, g.recovery_limit)
    return mp


def evaluate_daily_pnl(context):
    """风控·空仓策略（盘后）：统计连盈/连亏，触发对应空仓与阶梯回补。"""
    total = context.portfolio.total_value
    today = context.current_dt.date()

    # 1) 当日盈亏判定与连胜/连亏计数（平盘视为中断；回补期间盈利不计入连胜）
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

    # 2) 连续盈利 WIN_STREAK 天 → 空仓 WIN_COOL_DAYS 天
    if g.profit_streak >= WIN_STREAK:
        g.profit_streak = 0
        try:
            days = get_trade_days(start_date=today, count=WIN_COOL_DAYS + 1)
            last = days[-1]
            g.win_cool_until = last.date() if hasattr(last, 'date') else last
        except Exception:
            g.win_cool_until = today + timedelta(days=WIN_COOL_DAYS)
        log.info('连续盈利 %d 天，触发空仓 %d 天' % (WIN_STREAK, WIN_COOL_DAYS))

    # 3) 连续亏损 LOSS_STREAK 天 → 空仓 1 天，随后阶梯回补（1→2→3，每盈利一天加一只）
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


def escalate_penalty():
    """风控·止损级联：可持仓数量级联下调（3→2→1→0），维持天数指数递增（1→2→4）。"""
    if g.penalty_level == 0:
        g.penalty_level = 1
        g.penalty_left = PENALTY_DAYS[0]
    elif g.penalty_level == 1:
        g.penalty_level = 2
        g.penalty_left = PENALTY_DAYS[1]
    elif g.penalty_level == 2:
        g.penalty_level = 3
        g.penalty_left = PENALTY_DAYS[2]
    # 档位3：已空仓，无持仓可再止损
    g.max_positions = MAX_POSITIONS - g.penalty_level


def intraday_stop_loss(context, data):
    """风控·严格止损（盘中每bar）：持有股票当日涨幅跌破 MIN_GAIN 即全部卖出；持有满 MIN_HOLD_DAYS 天才触发。"""
    positions = context.portfolio.positions
    holding = [s for s in positions if positions[s].total_amount > 0]
    if not holding:
        return
    current = get_current_data()

    for s in holding:
        entry = g.entry_date.get(s)
        if entry is not None and held_trading_days(context, entry) < MIN_HOLD_DAYS:
            continue   # 持有未满3个交易日，暂不止损
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
    """风控·移动止盈：相对成本涨 >5% 后，从动态峰值回落 2% 立即全部卖出。"""
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
                g.entry_date.pop(s, None)
                g.tp_date[s] = today   # 记录止盈日期，冷却期内禁止买入


# ================================================================
# 第二层：选股
# ================================================================

def build_universe(context):
    """选股·股票池：沪深A股（排除ST/退市、上市不足60日、已退市、停牌）。"""
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
    """选股·候选：10:30 时成交额最大且涨幅为正的前 CANDIDATE_N 个申万二级行业，各行业取成交额前 PER_SECTOR_N 只。"""
    if not universe:
        return []
    universe_set = set(universe)
    today = context.current_dt.date()
    prev = context.previous_date

    # 1) 申万二级行业 → 成分股（与股票池取交集）
    try:
        industries = get_industries(name='sw_l2', date=prev)
    except Exception:
        industries = None
    if industries is None or len(industries) == 0:
        return []

    industry_stocks = {}
    for code in industries.index:
        try:
            members = get_industry_stocks(code, date=prev)
        except Exception:
            continue
        members = [s for s in members if s in universe_set]
        if members:
            industry_stocks[code] = members
    if not industry_stocks:
        return []

    # 2) 昨收（用于涨幅），并缓存供盘中严格止损
    pre_close = {}
    df_close = get_price(universe, end_date=prev, count=1,
                         fields=['close'], panel=False, skip_paused=True)
    if df_close is not None and len(df_close) > 0:
        df_close = df_close.reset_index()
        if 'code' in df_close.columns and 'close' in df_close.columns:
            pre_close = dict(zip(df_close['code'], df_close['close']))
            g.pre_close.update(pre_close)

    # 3) 当日分钟数据（回测引擎截断到当前 10:30）→ 每只股票成交额 + 最新价
    df = get_price(universe, start_date=today, end_date=today,
                   frequency='1m', fields=['money', 'close'],
                   panel=False, skip_paused=True)
    if df is None or len(df) == 0:
        return []
    df = df.reset_index()   # 列：time, code, money, close
    if 'code' not in df.columns or 'money' not in df.columns or 'close' not in df.columns:
        return []

    amount = {}
    last_price = {}
    for code, sub in df.groupby('code'):
        amount[code] = sub['money'].sum()
        last_price[code] = sub.iloc[-1]['close']

    # 4) 板块成交额 + 涨幅（成交额加权）；剔除涨幅非正；按成交额取前 CANDIDATE_N
    scored = []
    for members in industry_stocks.values():
        tot_amt = 0.0
        tot_ret = 0.0
        for s in members:
            amt = amount.get(s, 0.0)
            pre = pre_close.get(s, 0.0)
            last = last_price.get(s, 0.0)
            if amt <= 0 or pre <= 0:
                continue
            tot_amt += amt
            tot_ret += amt * (last / pre - 1)
        if tot_amt <= 0:
            continue
        sector_gain = tot_ret / tot_amt   # 板块成交额加权涨幅
        if sector_gain <= 0:
            continue   # 涨幅非正，剔除
        scored.append((tot_amt, members))

    scored.sort(key=lambda x: x[0], reverse=True)

    # 5) 前 CANDIDATE_N 板块内，各取成交额前 PER_SECTOR_N 只
    candidates = []
    for _, members in scored[:CANDIDATE_N]:
        valid = [s for s in members if amount.get(s, 0.0) > 0]
        valid.sort(key=lambda s: amount.get(s, 0.0), reverse=True)
        candidates.extend(valid[:PER_SECTOR_N])
    return candidates


# ================================================================
# 第三层：仓位控制（名额分配 / 单只仓位 / 下单补位）
# ================================================================

def single_target_value(exposure, max_pos):
    """仓位控制·单只目标仓位 = min(单只上限 20%, 总仓位 / 可持仓数)。"""
    return min(MAX_SINGLE, exposure / max_pos)


def plan_positions(context, holding, qualified, candidates, max_pos):
    """仓位控制·名额分配：被迫持有 + 合格持仓 + 按热度补齐，返回目标持仓。

    forced：持有未满3天、暂不能止损的（被迫继续持有，占用名额）
    keep：合格持仓 + 按热度补齐，总持仓 <= max_pos
    """
    today = context.current_dt.date()

    forced = [s for s in holding
              if s not in qualified
              and g.entry_date.get(s) is not None
              and held_trading_days(context, g.entry_date[s]) < MIN_HOLD_DAYS]
    keep = [s for s in holding if s in qualified]

    room = max_pos - len(forced)
    if room < 0:
        room = 0
    if len(keep) > room:                       # 名额不足：按热度砍掉靠后的合格持仓
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

    # 名额不足时，卖出不在目标内、可卖的合格持仓
    for s in holding:
        if s in qualified and s not in keep:
            order_target_value(s, 0)
            g.peak_price.pop(s, None)
            g.entry_date.pop(s, None)
            log.info('超出可持仓名额卖出 %s' % s)

    return target


def execute_orders(context, target, candidates, holding, max_pos):
    """仓位控制·下单：新进直接建满；买不起一手时用下一个候选补位。"""
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
            # 已有持仓：只降不升（指数降仓位时下调）
            if cur_ratio > single_target:
                order_target_value_with_tolerance(context, s, total * single_target)
            continue
        # 新进标的：直接建满单只目标仓位，并记录建仓日
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
    """仓位控制·节前买入：节前最后1个交易日 14:30 后，按候选顺序买入过节仓位。"""
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


# ================================================================
# 下单工具与辅助
# ================================================================

def order_target_value_with_tolerance(context, security, target):
    """目标价值下单，带 1% 容差；开仓金额不足一手时跳过。返回是否已发起下单。"""
    pos = context.portfolio.positions.get(security)
    cur = pos.value if pos is not None else 0.0
    if context.portfolio.total_value > 0 and \
            abs(cur - target) / context.portfolio.total_value < 0.01:
        return False

    # 开仓（当前无持仓）且目标金额不足一手（100股）：跳过，避免“数量不能小于100”
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

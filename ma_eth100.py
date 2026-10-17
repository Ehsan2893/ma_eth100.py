# Backtest: ETH only, 5m, risk:reward 1:2, the most recent N trades. NO real money.
# SELF-CONTAINED (does not need mabot.py).
# MA 9/26 (OHLC4) cross + pullback strategy. Data: OKX public candles.
#   python ma_eth100.py 180 100   -> fetch 180 days of history, report the last 100 trades
import requests, time, sys, csv
from datetime import datetime, timezone

OKX = "https://www.okx.com/api/v5"
LEVS = [1, 3, 5, 10, 15]
RR = 2.0

CFG = dict(
    fast=9, slow=26, atr_n=14,
    atr_buf=0.1,         # stop = slow MA -/+ this many ATRs
    expire=30,           # candles to wait for the pullback after a cross
    cap=144,             # max candles in a trade (12h), then close at market
    warmup=60,           # ignore signals in the first candles
    fee_pct=0.05,        # taker fee per side
    slip_pct=0.02,       # slippage per side
    margin=20.0,         # fake dollars of own money per trade
    start_balance=100.0,
    maint_pct=0.5,       # maintenance margin for the liquidation check
)


def get_json(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=25,
                             headers={"User-Agent": "paperbot"})
            if r.status_code == 200:
                return r.json()
            print("HTTP", r.status_code, url[-30:])
            if r.status_code in (429, 403):
                time.sleep(5)
        except Exception as e:
            print("net error:", str(e)[:80])
            time.sleep(3)
    return None


def parse(c):
    return dict(ts=int(c[0]) // 1000, o=float(c[1]), h=float(c[2]),
                l=float(c[3]), c=float(c[4]), v=float(c[5]))


def fetch_history(inst, days):
    need = days * 288
    out, after = [], None
    while len(out) < need:
        p = {"instId": inst, "bar": "5m", "limit": 100}
        if after:
            p["after"] = after
        d = (get_json(OKX + "/market/history-candles", p) or {}).get("data") or []
        time.sleep(0.15)
        if not d:
            break
        out.extend(parse(c) for c in d if len(c) >= 9 and c[8] == "1")
        after = d[-1][0]
    seen, rows = set(), []
    for c in sorted(out, key=lambda x: x["ts"]):
        if c["ts"] not in seen:
            seen.add(c["ts"])
            rows.append(c)
    return rows[-need:]


# ---------- indicators and signals ----------
def sma(vals, n):
    out, run = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def atr(cs, n):
    out = [None] * len(cs)
    tr = [None] + [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]),
                       abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    if len(cs) <= n:
        return out
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(cs)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def find_signals(cs):
    o4 = [(c["o"] + c["h"] + c["l"] + c["c"]) / 4 for c in cs]
    f, s, a = sma(o4, CFG["fast"]), sma(o4, CFG["slow"]), atr(cs, CFG["atr_n"])
    sigs, d, ci, left = [], 0, 0, False
    for i in range(1, len(cs)):
        if None in (f[i], s[i], f[i - 1], s[i - 1], a[i]):
            continue
        if f[i - 1] <= s[i - 1] and f[i] > s[i]:
            d, ci, left = 1, i, False
            continue
        if f[i - 1] >= s[i - 1] and f[i] < s[i]:
            d, ci, left = -1, i, False
            continue
        if d == 0:
            continue
        if i - ci > CFG["expire"] or (d == 1 and f[i] <= s[i]) or (d == -1 and f[i] >= s[i]):
            d = 0
            continue
        c = cs[i]
        buf = CFG["atr_buf"] * a[i]
        if d == 1:
            if not left:
                left = c["l"] > f[i]
            elif c["c"] <= s[i]:
                d = 0
            elif c["l"] <= f[i]:
                sigs.append(dict(i=i, side=1, sl=s[i] - buf))
                d = 0
        else:
            if not left:
                left = c["h"] < f[i]
            elif c["c"] >= s[i]:
                d = 0
            elif c["h"] >= f[i]:
                sigs.append(dict(i=i, side=-1, sl=s[i] + buf))
                d = 0
    return sigs


def simulate(cs, e, side, sl, rr):
    entry = cs[e]["o"]
    risk = abs(entry - sl)
    if risk <= 0 or (side == 1 and sl >= entry) or (side == -1 and sl <= entry):
        return None
    tp = entry + side * rr * risk
    last = min(e + CFG["cap"], len(cs) - 1)
    ex, reason, xi = None, None, None
    for k in range(e, last + 1):
        c = cs[k]
        if k > e:
            if (side == 1 and c["o"] <= sl) or (side == -1 and c["o"] >= sl):
                ex, reason, xi = c["o"], "SL", k
                break
            if (side == 1 and c["o"] >= tp) or (side == -1 and c["o"] <= tp):
                ex, reason, xi = c["o"], "TP", k
                break
        hit_sl = c["l"] <= sl if side == 1 else c["h"] >= sl
        hit_tp = c["h"] >= tp if side == 1 else c["l"] <= tp
        if hit_sl:
            ex, reason, xi = sl, "SL", k
            break
        if hit_tp:
            ex, reason, xi = tp, "TP", k
            break
        if k == e + CFG["cap"]:
            ex, reason, xi = c["c"], "TIME", k
            break
    if ex is None:
        return {"open": True}
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    gross = side * (ex / entry - 1) * 100
    risk_pct = risk / entry * 100
    return dict(entry=entry, sl=sl, tp=tp, exit=ex, reason=reason, exit_idx=xi,
                risk_pct=risk_pct, gross_pct=gross, net_pct=gross - cost,
                net_R=(gross - cost) / risk_pct)


def lev_pnl(margin, lev, net_pct, risk_pct):
    if lev > 1 and risk_pct >= 100.0 / lev - CFG["maint_pct"]:
        return -margin
    return max(margin * lev * net_pct / 100, -margin)


def equity(ts, lev):
    cash, peak, maxdd, taken, wins = CFG["start_balance"], CFG["start_balance"], 0.0, 0, 0
    m = CFG["margin"]
    opened = []

    def settle(upto):
        nonlocal cash, peak, maxdd, opened
        opened.sort(key=lambda x: x[0])
        keep, closed = [], 0
        total = len(opened)
        for ex, pnl in opened:
            if ex <= upto:
                cash += m + pnl
                closed += 1
                eq = cash + m * (total - closed)
                peak = max(peak, eq)
                maxdd = max(maxdd, (peak - eq) / peak * 100)
            else:
                keep.append((ex, pnl))
        opened = keep

    for t in sorted(ts, key=lambda x: x["entry_ts"]):
        settle(t["entry_ts"])
        if cash >= m:
            cash -= m
            pnl = lev_pnl(m, lev, t["net_pct"], t["risk_pct"])
            opened.append((t["exit_ts"], pnl))
            taken += 1
            wins += 1 if pnl > 0 else 0
    settle(10 ** 12)
    return dict(final=cash, ret=(cash / CFG["start_balance"] - 1) * 100,
                dd=maxdd, taken=taken, wins=wins)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def stats(ts, label):
    n = len(ts)
    if n == 0:
        print("%s: no trades" % label)
        return
    wins = [t for t in ts if t["net_pct"] > 0]
    gp = sum(t["net_pct"] for t in wins)
    gl = -sum(t["net_pct"] for t in ts if t["net_pct"] <= 0)
    avg = sum(t["net_pct"] for t in ts) / n
    stop = sum(t["risk_pct"] for t in ts) / n
    print("%s: n=%d win=%.0f%% avg_net/trade=%+.3f%% PF=%.2f avg_stop=%.2f%% TP=%d SL=%d TIME=%d" % (
        label, n, 100 * len(wins) / n, avg, gp / gl if gl else 99.0, stop,
        sum(1 for t in ts if t["reason"] == "TP"), sum(1 for t in ts if t["reason"] == "SL"),
        sum(1 for t in ts if t["reason"] == "TIME")))


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    last_n = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    cs = fetch_history("ETH-USDT-SWAP", days)
    print("ETH candles: %d (%s -> %s)" % (len(cs), iso(cs[0]["ts"]) if cs else "-", iso(cs[-1]["ts"]) if cs else "-"))
    if len(cs) < 1000:
        print("not enough data")
        return
    sigs = find_signals(cs)
    trades, busy = [], -1
    for sg in sigs:
        e = sg["i"] + 1
        if e >= len(cs) or e <= busy or sg["i"] < CFG["warmup"]:
            continue
        t = simulate(cs, e, sg["side"], sg["sl"], RR)
        if t is None or t.get("open"):
            continue
        busy = t["exit_idx"]
        t.update(side=sg["side"], entry_ts=cs[e]["ts"], exit_ts=cs[t["exit_idx"]]["ts"])
        trades.append(t)
    print("signals=%d, completed trades in the period=%d" % (len(sigs), len(trades)))
    use = trades[-last_n:]
    if len(use) < last_n:
        print("WARNING: only %d trades available (asked for %d). Use more days." % (len(use), last_n))
    if not use:
        return
    print("trades analysed: %s -> %s" % (iso(use[0]["entry_ts"]), iso(use[-1]["exit_ts"])))
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    stop = sum(t["risk_pct"] for t in use) / len(use)
    c = cost / stop
    print("cost per trade %.2f%% of position = %.2f R (avg stop %.2f%%)" % (cost, c, stop))
    print("break-even win rate with RR 1:%.0f after costs = %.0f%% (without costs 33%%)" % (RR, 100 * (1 + c) / (RR + 1)))
    stats(use, "ALL")
    stats([t for t in use if t["side"] > 0], "LONG")
    stats([t for t in use if t["side"] < 0], "SHORT")
    half = len(use) // 2
    stats(use[:half], "FIRST HALF")
    stats(use[half:], "SECOND HALF")
    print("equity ($%.0f start, $%.0f margin per trade):" % (CFG["start_balance"], CFG["margin"]))
    for lev in LEVS:
        q = equity(use, lev)
        print("  x%-2d final $%.2f  return %+.1f%%  max drawdown %.1f%%" % (lev, q["final"], q["ret"], q["dd"]))
    with open("ma_eth100_trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_time", "exit_time", "side", "entry", "sl", "tp", "exit",
                    "reason", "stop_pct", "net_pct", "net_R"])
        for t in use:
            w.writerow([iso(t["entry_ts"]), iso(t["exit_ts"]), "LONG" if t["side"] > 0 else "SHORT",
                        t["entry"], round(t["sl"], 4), round(t["tp"], 4), t["exit"], t["reason"],
                        round(t["risk_pct"], 3), round(t["net_pct"], 3), round(t["net_R"], 3)])


if __name__ == "__main__":
    main()

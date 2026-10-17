# Backtest: ETH only, 5m, risk:reward 1:2, the most recent N trades. NO real money.
# Uses the strategy engine in mabot.py (same file folder).
#   python ma_eth100.py 180 100   -> fetch 180 days of history, report the last 100 trades
import sys, csv
from datetime import datetime, timezone
import mabot as m

RR = 2.0


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
    cs = m.fetch_history("ETH-USDT-SWAP", days)
    print("ETH candles: %d (%s -> %s)" % (len(cs), iso(cs[0]["ts"]) if cs else "-", iso(cs[-1]["ts"]) if cs else "-"))
    if len(cs) < 1000:
        print("not enough data")
        return
    sigs = m.find_signals(cs)
    trades, busy = [], -1
    for sg in sigs:
        e = sg["i"] + 1
        if e >= len(cs) or e <= busy or sg["i"] < m.CFG["warmup"]:
            continue
        t = m.simulate(cs, e, sg["side"], sg["sl"], RR)
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
    cost = 2 * (m.CFG["fee_pct"] + m.CFG["slip_pct"])
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
    print("equity ($%.0f start, $%.0f margin per trade):" % (m.CFG["start_balance"], m.CFG["margin"]))
    for lev in m.LEVS:
        q = m.equity(use, lev)
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

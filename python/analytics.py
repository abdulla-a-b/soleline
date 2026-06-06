#!/usr/bin/env python3
"""
SOLELINE — analytics engine
===========================
Reads a board snapshot (lines / orders / production) and produces a
forward-looking insights file the dashboard can surface on the Command Deck.

INPUT  (any one of):
  * data/board.json          — { "lines":[...], "orders":[...], "prod":[...] }
  * --url <Apps Script /exec> — pulls the live snapshot via ?action=pull
OUTPUT:
  * data/insights.json       — capacity risk, completion forecast, headline KPIs

The domain maths here MIRROR the front-end (index.html) exactly so the
numbers reconcile:
  * Friday is the only weekly off  (Bangladesh RMG convention)
  * need_to_produce = order pairs - produced
  * a line is over-booked on a day when committed daily run-rate > line capacity
  * required_rate   = ceil(need / business_days_left)

Run locally:      python analytics.py
Run in CI:        python analytics.py --url "$SOLELINE_URL"
"""

import argparse
import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


# ----------------------------------------------------------------------------- helpers
def parse_d(s):
    """Accept 'YYYY-MM-DD' (or a date/datetime) and return a date."""
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def business_days_between(a, b):
    """Inclusive day count, Friday (weekday 4) treated as the only weekly off."""
    a, b = parse_d(a), parse_d(b)
    if b < a:
        return 1
    c, d = 0, a
    while d <= b:
        if d.weekday() != 4:  # Mon=0 ... Fri=4 ... Sun=6
            c += 1
        d += timedelta(days=1)
    return max(1, c)


def daterange(a, b):
    d = parse_d(a)
    end = parse_d(b)
    while d <= end:
        yield d
        d += timedelta(days=1)


# ----------------------------------------------------------------------------- load
def load_board(args):
    if args.url:
        url = args.url + ("&" if "?" in args.url else "?") + "action=pull"
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    src = Path(args.input) if args.input else (DATA / "board.json")
    if not src.exists():
        print(f"[analytics] no input at {src} and no --url given; nothing to do.")
        sys.exit(0)
    return json.loads(src.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- core calcs
def index_production(prod):
    by_order = {}
    for p in prod:
        oid = p.get("orderId")
        by_order[oid] = by_order.get(oid, 0) + int(p.get("pairs") or 0)
    return by_order


def committed_on_day(orders, line_id, d):
    total = 0
    for o in orders:
        if o.get("lineId") != line_id:
            continue
        if parse_d(o["start"]) <= d <= parse_d(o["delivery"]):
            total += int(o.get("daily") or 0)
    return total


def capacity_risk(lines, orders, horizon_days=30):
    """For each line scan the next `horizon_days` and flag over-booked days."""
    today = date.today()
    out = []
    for ln in lines:
        cap = int(ln.get("cap") or 0)
        breaches, peak = [], 0
        for i in range(horizon_days):
            d = today + timedelta(days=i)
            if d.weekday() == 4:  # Friday off
                continue
            committed = committed_on_day(orders, ln["id"], d)
            peak = max(peak, committed)
            if cap and committed > cap:
                breaches.append(
                    {"date": d.isoformat(), "committed": committed,
                     "cap": cap, "over": committed - cap}
                )
        active = any(parse_d(o["start"]) <= today + timedelta(days=horizon_days)
                     and parse_d(o["delivery"]) >= today
                     for o in orders if o.get("lineId") == ln["id"])
        if breaches:
            status = "overbooked"
        elif not active:
            status = "vacant"
        elif cap and peak >= 0.9 * cap:
            status = "near_full"
        else:
            status = "healthy"
        out.append({
            "lineId": ln["id"], "name": ln.get("name"), "type": ln.get("type"),
            "cap": cap, "peakCommitted": peak,
            "utilisation": round((peak / cap) * 100) if cap else 0,
            "status": status, "breachDays": len(breaches),
            "firstBreach": breaches[0] if breaches else None,
        })
    return out


def completion_forecast(orders, produced_by):
    today = date.today()
    out = []
    for o in orders:
        pairs = int(o.get("pairs") or 0)
        done = int(produced_by.get(o["id"], 0))
        need = max(0, pairs - done)
        delivery = parse_d(o["delivery"])
        days_left = 0 if today > delivery else business_days_between(today, delivery)
        required = (need + days_left - 1) // days_left if days_left else need
        daily = int(o.get("daily") or 0)
        # projected finish at the planned daily run-rate (skipping Fridays)
        proj_finish, remaining, d = None, need, today
        if need <= 0:
            status = "complete"
            proj_finish = "done"
        else:
            guard = 0
            while remaining > 0 and guard < 800:
                if d.weekday() != 4:
                    remaining -= daily
                    if remaining <= 0:
                        proj_finish = d.isoformat()
                        break
                d += timedelta(days=1)
                guard += 1
            if today > delivery:
                status = "overdue"
            elif required > daily * 1.05:
                status = "at_risk"   # planned rate can't clear the backlog in time
            else:
                status = "on_track"
        pct = round((done / pairs) * 100) if pairs else 0
        out.append({
            "orderId": o["id"], "po": o.get("po"), "buyer": o.get("buyer"),
            "article": o.get("article"), "lineId": o.get("lineId"),
            "pairs": pairs, "produced": done, "needToProduce": need,
            "pctComplete": pct, "daysLeft": days_left,
            "plannedDaily": daily, "requiredDaily": required,
            "projectedFinish": proj_finish, "delivery": delivery.isoformat(),
            "status": status,
        })
    return out


def headline(lines_risk, orders_fc, orders, produced_by):
    return {
        "totalOrders": len(orders),
        "totalPairs": sum(int(o.get("pairs") or 0) for o in orders),
        "totalProduced": sum(produced_by.values()),
        "needToProduce": sum(o["needToProduce"] for o in orders_fc),
        "overbookedLines": sum(1 for l in lines_risk if l["status"] == "overbooked"),
        "vacantLines": sum(1 for l in lines_risk if l["status"] == "vacant"),
        "ordersAtRisk": sum(1 for o in orders_fc if o["status"] in ("at_risk", "overdue")),
    }


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="SOLELINE analytics")
    ap.add_argument("--input", help="path to board.json")
    ap.add_argument("--url", help="Apps Script /exec URL (pulls live snapshot)")
    ap.add_argument("--horizon", type=int, default=30, help="forward scan days")
    args = ap.parse_args()

    board = load_board(args)
    lines = board.get("lines", [])
    orders = board.get("orders", [])
    prod = board.get("prod", board.get("production", []))

    produced_by = index_production(prod)
    lines_risk = capacity_risk(lines, orders, args.horizon)
    orders_fc = completion_forecast(orders, produced_by)

    insights = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "horizonDays": args.horizon,
        "headline": headline(lines_risk, orders_fc, orders, produced_by),
        "lineCapacity": lines_risk,
        "orderForecast": orders_fc,
        "alerts": (
            [f"{l['name']} over-booked on {l['breachDays']} day(s); first breach "
             f"{l['firstBreach']['date']} (+{l['firstBreach']['over']} pairs/day)"
             for l in lines_risk if l["status"] == "overbooked"]
            + [f"{o['po']} ({o['buyer']}) {o['status'].replace('_',' ')} — needs "
               f"{o['requiredDaily']}/day vs planned {o['plannedDaily']}"
               for o in orders_fc if o["status"] in ("at_risk", "overdue")]
        ),
    }

    out = DATA / "insights.json"
    out.write_text(json.dumps(insights, indent=2), encoding="utf-8")
    h = insights["headline"]
    print(f"[analytics] wrote {out}")
    print(f"[analytics] orders={h['totalOrders']} pairs={h['totalPairs']:,} "
          f"need={h['needToProduce']:,} overbooked_lines={h['overbookedLines']} "
          f"vacant_lines={h['vacantLines']} at_risk={h['ordersAtRisk']}")


if __name__ == "__main__":
    main()

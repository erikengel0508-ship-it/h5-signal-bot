#!/usr/bin/env python3
"""H5 signal bot: ICT Silver Bullet on gold (target = 0.5 x stop distance), push messages via ntfy.

Runs once per weekday (GitHub Actions), from before 09:00 until 12:00 New York time. Prices: public Swissquote quote
feed (XAU/USD bid), polled every few seconds and aggregated into 1-minute bars. Rule identical to the research repo's
gold_setups.silver_bullet_live(tp_r=0.5) and the TradingView indicator h5_silver_bullet.pine:
  range = high/low 09:00-09:59 NY; window 10:00-10:59 NY: sweep of the range low (long) / high (short), then the first
  fair value gap (long: low[k] > high[k-2]); limit order at high[k-2] (short: low[k-2]); stop = extreme since the sweep;
  target = entry +/- 0.5 x stop distance; invalid (side done) if the entry is not inside the range. Unfilled orders are
  cancelled at 11:00 NY; time exit with the close of the 11:59 NY bar. Both sides run at the same time, the first filled
  order counts, the other one is cancelled.
Risk status: risk_status.json next to this file (written by the research repo's src/risk/engine.py, ADR-0030): allowed, lots,
stage, reason. When H5 is paused, setups are sent as "nur Modell, kein Trade" with low priority; a status older than 14 days is
flagged. Without the file the bot behaves as before (demo).
Standard library only. Environment: NTFY_TOPIC (push channel; without it messages are only printed).
Usage: python3 h5_bot.py            (live run for today)
       python3 h5_bot.py --test     (sends one test message and exits)
"""

import json
import os
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DE = ZoneInfo("Europe/Berlin")
FEED = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
POLL_SECONDS = 4
TP_R = 0.5
COST = 0.42                       # USD per oz round trip at Tag Markets (spread 0.32 + commission 0.10)
FEED_ALARM_SECONDS = 120
RISK_FILE = Path(__file__).resolve().parent / "risk_status.json"
RISK_STALE_DAYS = 14
START_NY, END_NY = (8, 58), (12, 1)


def ny_minute(t: datetime) -> int:
    x = t.astimezone(NY)
    return x.hour * 60 + x.minute


def de_clock(t: datetime, hour: int, minute: int) -> str:
    """German clock time of hour:minute New York on the NY date of t."""
    d = t.astimezone(NY)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(DE).strftime("%H:%M")


def fmt(x: float) -> str:
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class Side:
    def __init__(self, direction: int):
        self.dir = direction
        self.state = 0            # 0 waiting for sweep, 1 looking for gap, 2 order pending, 3 done, 4 filled
        self.since_sweep = 0
        self.ext = None
        self.level = self.stop = self.target = None
        self.k = None

    @property
    def order(self) -> str:
        return "BUY LIMIT" if self.dir > 0 else "SELL LIMIT"


class H5Live:
    """Bar-by-bar state machine. on_bar() takes closed 1-minute bars (UTC start time) and returns events."""

    def __init__(self, tp_r: float = TP_R, cost: float = COST):
        self.tp_r, self.cost = tp_r, cost
        self.day = None
        self.n = 0
        self.hist = deque(maxlen=3)
        self._reset()

    def _reset(self):
        self.ph = self.pl = None
        self.pre_n = 0
        self.L, self.S = Side(1), Side(-1)
        self.traded = False
        self.trade = None
        self.range_sent = False

    def _scan(self, s: Side, t, h, l) -> Optional[Dict]:
        if s.state == 0:
            if (s.dir > 0 and l < self.pl) or (s.dir < 0 and h > self.ph):
                s.state, s.since_sweep, s.ext = 1, 0, (l if s.dir > 0 else h)
            return None
        if s.state != 1:
            return None
        s.since_sweep += 1
        s.ext = min(s.ext, l) if s.dir > 0 else max(s.ext, h)
        if s.since_sweep < 2:
            return None
        b2 = self.hist[-2]                                     # bar k-2 (hist holds the bars before the current one)
        gap = (l > b2["h"]) if s.dir > 0 else (h < b2["l"])
        if not gap:
            return None
        lv = b2["h"] if s.dir > 0 else b2["l"]
        t0 = self.ph if s.dir > 0 else self.pl
        if (t0 - lv) * s.dir <= 0 or (lv - s.ext) * s.dir <= 0:
            s.state = 3
            return None
        risk = (lv - s.ext) * s.dir
        s.level, s.stop, s.target, s.k, s.state = lv, s.ext, lv + s.dir * self.tp_r * risk, self.n, 2
        return {"kind": "pending", "t": t, "side": s.dir, "order": s.order, "entry": lv, "stop": s.stop, "target": s.target, "risk": risk}

    def _cancel_pending(self, t) -> List[Dict]:
        ev = []
        for s in (self.L, self.S):
            if s.state == 2:
                s.state = 3
                ev.append({"kind": "cancel", "t": t, "side": s.dir, "order": s.order, "entry": s.level})
        return ev

    def close_open_trade(self, t, px: float) -> List[Dict]:
        """Time exit when the 11:59 bar never arrived (feed gap)."""
        if self.trade is None:
            return []
        return [self._exit(t, px, "time")]

    def _exit(self, t, px, why) -> Dict:
        tr = self.trade
        net = tr["side"] * (px - tr["entry"]) - self.cost
        self.trade = None
        return {"kind": "exit", "t": t, "side": tr["side"], "entry": tr["entry"], "exit": px, "reason": why, "net": net}

    def on_bar(self, t: datetime, o: float, h: float, l: float, c: float) -> List[Dict]:
        ev: List[Dict] = []
        tn = t.astimezone(NY)
        day, m = tn.date(), tn.hour * 60 + tn.minute
        if day != self.day:
            if self.trade is not None:
                ev.append(self._exit(t, self.hist[-1]["c"] if self.hist else o, "time"))
            self.day = day
            self._reset()
        weekday = tn.weekday() < 5
        if weekday and 540 <= m < 600:
            self.ph = h if self.ph is None else max(self.ph, h)
            self.pl = l if self.pl is None else min(self.pl, l)
            self.pre_n += 1
            if m == 599 and self.pre_n >= 55:
                self.range_sent = True
                ev.append({"kind": "range", "t": t, "high": self.ph, "low": self.pl})
        if weekday and m >= 600 and not self.range_sent:
            self.range_sent = True
            ev.append({"kind": "range" if self.pre_n >= 55 else "no_range", "t": t, "high": self.ph, "low": self.pl, "n": self.pre_n})
        if weekday and m >= 660 and not self.traded:
            ev += self._cancel_pending(t)
        if weekday and 600 <= m < 660 and self.pre_n >= 55 and not self.traded:
            fL = self.L.state == 2 and l <= self.L.level
            fS = self.S.state == 2 and h >= self.S.level
            if fL or fS:
                long_wins = fL and (not fS or self.L.k <= self.S.k)
                w, other = (self.L, self.S) if long_wins else (self.S, self.L)
                self.trade = {"side": w.dir, "entry": w.level, "stop": w.stop, "target": w.target, "bar": self.n}
                self.traded = True
                w.state = 4
                ev.append({"kind": "fill", "t": t, "side": w.dir, "order": w.order, "entry": w.level,
                           "cancel": ({"order": other.order, "entry": other.level} if other.state == 2 else None)})
                other.state = 3
            else:
                for s in (self.L, self.S):
                    e = self._scan(s, t, h, l)
                    if e:
                        ev.append(e)
            if m == 659 and not self.traded:
                ev += self._cancel_pending(t)
        if self.trade is not None:
            tr = self.trade
            hit_s = l <= tr["stop"] if tr["side"] > 0 else h >= tr["stop"]
            hit_t = h >= tr["target"] if tr["side"] > 0 else l <= tr["target"]
            if hit_s:
                gapped = self.n == tr["bar"] and (o < tr["stop"] if tr["side"] > 0 else o > tr["stop"])
                ev.append(self._exit(t, o if gapped else tr["stop"], "stop"))
            elif hit_t:
                ev.append(self._exit(t, tr["target"], "target"))
            elif m >= 719:
                ev.append(self._exit(t, c, "time"))
        self.hist.append({"h": h, "l": l, "c": c})
        self.n += 1
        return ev


def load_risk(path: Path = RISK_FILE) -> Optional[Dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def risk_line(risk: Optional[Dict], now: datetime) -> str:
    if not risk:
        return "Risk: kein Status (Demo)."
    age = (now - datetime.fromisoformat(risk["updated_utc"].replace("Z", "+00:00"))).days
    stale = f" Status {age} Tage alt - bitte Trades melden." if age > RISK_STALE_DAYS else ""
    if risk.get("allowed", True):
        return f"Risk: frei, {str(risk['lots']).replace('.', ',')} Lot ({risk['stage']}).{stale}"
    return f"Risk: PAUSIERT ({risk['stage']}) - {risk.get('reason', '')}. Keine Orders.{stale}"


def message(e: Dict, risk: Optional[Dict] = None) -> Dict[str, str]:
    """Push text (title, body, priority) for an event, adjusted to the risk status."""
    m = _message(e, risk)
    if risk and not risk.get("allowed", True) and e["kind"] in ("pending", "fill", "cancel", "exit"):
        m = {"title": m["title"] + " (nur Modell, kein Trade)", "priority": "low", "body": "H5 ist pausiert. " + m["body"]}
    return m


def _message(e: Dict, risk: Optional[Dict]) -> Dict[str, str]:
    t = e["t"]
    side = "Long" if e.get("side", 0) > 0 else "Short"
    if e["kind"] == "range":
        return {"title": "H5 Gold: Spanne steht", "priority": "default",
                "body": f"09-10 NY: Hoch {fmt(e['high'])} / Tief {fmt(e['low'])}. Setup-Fenster {de_clock(t, 10, 0)}-{de_clock(t, 11, 0)} Uhr. "
                        + risk_line(risk, t)}
    if e["kind"] == "no_range":
        return {"title": "H5 Gold: heute kein Setup", "priority": "low",
                "body": f"Nur {e['n']} von 60 Minuten Kursdaten fuer die Spanne 09-10 NY - heute keine Signale."}
    if e["kind"] == "pending":
        return {"title": f"H5 Gold: {e['order']} {fmt(e['entry'])}", "priority": "high",
                "body": f"{e['order']} {fmt(e['entry'])} | SL {fmt(e['stop'])} | TP {fmt(e['target'])} | Stop {fmt(e['risk'])} $"
                        + (f" | {str(risk['lots']).replace('.', ',')} Lot" if risk and risk.get("allowed", True) else "")
                        + f". Gueltig bis {de_clock(t, 11, 0)} Uhr, dann loeschen."}
    if e["kind"] == "fill":
        body = f"Modell-Order {e['order']} {fmt(e['entry'])} ausgeloest ({side})."
        if e["cancel"]:
            body += f" Jetzt die {e['cancel']['order']} {fmt(e['cancel']['entry'])} LOESCHEN."
        return {"title": "H5 Gold: Order ausgeloest", "priority": "high" if e["cancel"] else "default", "body": body}
    if e["kind"] == "cancel":
        return {"title": "H5 Gold: Order loeschen", "priority": "high",
                "body": f"{e['order']} {fmt(e['entry'])} nicht ausgeloest - Order jetzt loeschen."}
    if e["kind"] == "exit":
        if e["reason"] == "time":
            return {"title": "H5 Gold: JETZT schliessen", "priority": "urgent",
                    "body": f"Zeitausstieg {de_clock(t, 12, 0)} Uhr - offene {side}-Position jetzt schliessen (Modell {fmt(e['exit'])}, "
                            f"{e['net']:+.2f} $/oz netto)."}
        why = {"target": "Ziel (TP)", "stop": "Stop (SL)"}[e["reason"]]
        return {"title": f"H5 Gold: {why} erreicht", "priority": "default",
                "body": f"Modell-Trade {side} beendet: {why} bei {fmt(e['exit'])}, {e['net']:+.2f} $/oz netto."}
    return {"title": "H5 Gold", "priority": "default", "body": e.get("text", "")}


def fetch_bid(timeout: float = 5.0) -> Optional[float]:
    req = urllib.request.Request(FEED, headers={"User-Agent": "Mozilla/5.0 (h5-signal-bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    for platform in data:
        for p in platform.get("spreadProfilePrices", []):
            if p.get("spreadProfile") == "prime" and p.get("bid", 0) > 0:
                return float(p["bid"])
    for platform in data:
        for p in platform.get("spreadProfilePrices", []):
            if p.get("bid", 0) > 0:
                return float(p["bid"])
    return None


def send_ntfy(msg: Dict[str, str], topic: Optional[str]) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg['title']} | {msg['body']}", flush=True)
    if not topic:
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg["body"].encode("utf-8"), method="POST",
                                 headers={"Title": msg["title"].encode("ascii", "replace").decode(), "Priority": msg["priority"]})
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=10).read()
            return
        except Exception as ex:                                # noqa: BLE001
            print(f"ntfy error ({attempt + 1}/3): {ex}", flush=True)
            time.sleep(2)


class BarBuilder:
    """Aggregates price samples into 1-minute bars (UTC minute start)."""

    def __init__(self):
        self.minute = None
        self.o = self.h = self.l = self.c = None

    def add(self, t: datetime, px: float) -> Optional[Dict]:
        m = t.replace(second=0, microsecond=0)
        done = None
        if self.minute is not None and m != self.minute:
            done = self.close()
        if self.minute is None:
            self.minute, self.o, self.h, self.l, self.c = m, px, px, px, px
        else:
            self.h, self.l, self.c = max(self.h, px), min(self.l, px), px
        return done

    def close(self) -> Optional[Dict]:
        if self.minute is None:
            return None
        bar = {"t": self.minute, "o": self.o, "h": self.h, "l": self.l, "c": self.c}
        self.minute = None
        return bar


def run(now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), fetch: Callable[[], Optional[float]] = fetch_bid,
        send: Callable[[Dict[str, str]], None] = None, sleep: Callable[[float], None] = time.sleep,
        log_dir: Optional[Path] = Path("logs")) -> Dict:
    send = send or (lambda m: send_ntfy(m, os.environ.get("NTFY_TOPIC")))
    t = now()
    tn = t.astimezone(NY)
    start = tn.replace(hour=START_NY[0], minute=START_NY[1], second=0, microsecond=0)
    end = tn.replace(hour=END_NY[0], minute=END_NY[1], second=0, microsecond=0)
    log = {"date_ny": str(tn.date()), "bars": [], "events": [], "feed_errors": 0}
    if tn.weekday() >= 5 or tn >= end:
        print(f"nothing to do (NY {tn:%a %H:%M})", flush=True)
        return log
    if tn < start:
        wait = (start - tn).total_seconds()
        print(f"waiting {wait / 60:.0f} min until {start:%H:%M} NY", flush=True)
        sleep(wait)
    sm, bb = H5Live(), BarBuilder()
    risk = load_risk()
    log["risk"] = risk
    last_ok, alarmed = now(), False

    def handle(bar):
        log["bars"].append({**bar, "t": bar["t"].isoformat()})
        for e in sm.on_bar(bar["t"], bar["o"], bar["h"], bar["l"], bar["c"]):
            log["events"].append({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in e.items()})
            send(message(e, risk))

    while True:
        t = now()
        if t.astimezone(NY) >= end:
            break
        try:
            px = fetch()
        except Exception as ex:                                # noqa: BLE001
            px = None
            log["feed_errors"] += 1
            print(f"feed error: {ex}", flush=True)
        if px is not None:
            last_ok = t
            if alarmed:
                alarmed = False
                send({"title": "H5 Gold: Datenfeed wieder da", "priority": "default", "body": "Kurse kommen wieder an."})
            bar = bb.add(t, px)
            if bar:
                handle(bar)
        elif not alarmed and (t - last_ok).total_seconds() > FEED_ALARM_SECONDS and 540 <= ny_minute(t) < 720:
            alarmed = True
            send({"title": "H5 Gold: Datenfeed gestoert", "priority": "high",
                  "body": "Seit 2 Minuten keine Kurse - Signale koennen fehlen. Offene Orders selbst im Blick behalten."})
        # close the running minute on time even when the next sample is late
        if bb.minute is not None and (now() - bb.minute).total_seconds() >= 61:
            handle(bb.close())
        sleep(POLL_SECONDS)
    bar = bb.close()
    if bar:
        handle(bar)
    last_px = log["bars"][-1]["c"] if log["bars"] else None
    for e in (sm.close_open_trade(now(), last_px) if last_px is not None else []):
        log["events"].append({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in e.items()})
        send(message(e, risk))
    if log_dir is not None:
        log_dir.mkdir(exist_ok=True)
        (log_dir / f"{log['date_ny']}.json").write_text(json.dumps(log, indent=1, default=str))
    print(f"done: {len(log['bars'])} bars, {len(log['events'])} events, {log['feed_errors']} feed errors", flush=True)
    return log


if __name__ == "__main__":
    if "--test" in sys.argv:
        px = fetch_bid()
        send_ntfy({"title": "H5 Gold: Test", "priority": "default",
                   "body": f"Testnachricht - der Bot erreicht dein Handy. Gold aktuell {fmt(px) if px else 'nicht abrufbar'}."},
                  os.environ.get("NTFY_TOPIC"))
    else:
        run()

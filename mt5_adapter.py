"""MetaTrader 5 adapter: feed, broker, spec loader. Logic is unit-tested against a FAKE terminal only.
It has NOT been run against a real MT5 terminal: validate on a DEMO account first.
Real-account trading additionally requires allow_live=True and env APEX_I_UNDERSTAND_LIVE_RISK=yes. Validate on a demo account at your broker before going live."""
from __future__ import annotations
import logging, os, threading, time
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from .data import Candle, Quote
from .execution import Broker, OrderRequest, OrderResult
from .instruments import InstrumentSpec

log = logging.getLogger("apex.mt5")
TF_NAMES = {60: "M1", 300: "M5", 900: "M15", 1800: "M30", 3600: "H1", 14400: "H4", 86400: "D1"}


def load_mt5(login=None, server=None, password_env="MT5_PASSWORD", path=None):
    try: import MetaTrader5 as mt5  # type: ignore
    except ImportError as e: raise RuntimeError("MetaTrader5 package not installed (Windows + MT5 terminal required)") from e
    kw = {k: v for k, v in dict(login=login, server=server, password=os.environ.get(password_env), path=path).items() if v}
    if not mt5.initialize(**kw): raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    return mt5


def spec_from_mt5(mt5, symbol: str) -> InstrumentSpec:
    """Broker-verified spec. tick_value is in ACCOUNT currency, so q2a must be 1.0 for these specs."""
    if not mt5.symbol_select(symbol, True): raise RuntimeError(f"cannot select {symbol}")
    i = mt5.symbol_info(symbol)
    if i is None: raise RuntimeError(f"no symbol info for {symbol}")
    if i.trade_mode != getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4): raise RuntimeError(f"{symbol} not fully tradable (trade_mode={i.trade_mode})")
    fx = i.digits in (3, 5) or len(symbol) == 6
    pip = i.point * (10 if i.digits in (3, 5) else 1)
    return InstrumentSpec(symbol, "forex" if fx else "futures", i.trade_tick_size, i.trade_contract_size, i.trade_tick_value, pip,
                          getattr(i, "currency_base", ""), getattr(i, "currency_profit", "USD"), i.volume_step, i.volume_min,
                          i.volume_max, 0.0, 30.0, "BROKER" if not fx else "", True)


class MT5Feed:
    label = "MetaTrader 5"
    """Closed candles only (position 1 skips the forming bar: no repainting). Broker server time is converted to UTC."""
    def __init__(self, mt5, utc_offset_hours: Optional[float] = None):
        self.mt5, self.offset = mt5, utc_offset_hours

    def detect_offset(self, symbol: str) -> float:
        t = self.mt5.symbol_info_tick(symbol)
        if t is None: raise RuntimeError("no tick; cannot detect server offset (market closed?)")
        off = round((t.time - time.time()) / 3600.0)
        log.warning("auto-detected MT5 server UTC offset %+dh (set it explicitly in settings to be safe)", off)
        self.offset = off; return off

    def _utc(self, epoch) -> datetime:
        if self.offset is None: raise RuntimeError("server_utc_offset_hours not set")
        return datetime.fromtimestamp(epoch - self.offset * 3600, tz=timezone.utc)

    def candles(self, symbol: str, tf_seconds: int, count: int = 600) -> List[Candle]:
        tf = getattr(self.mt5, "TIMEFRAME_" + TF_NAMES[tf_seconds])
        r = self.mt5.copy_rates_from_pos(symbol, tf, 1, count)
        if r is None or len(r) == 0: raise RuntimeError(f"no rates for {symbol}: {self.mt5.last_error()}")
        return [Candle(self._utc(x["time"]), float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), float(x["tick_volume"])) for x in r]

    def quote(self, symbol: str) -> Optional[Quote]:
        t = self.mt5.symbol_info_tick(symbol)
        if t is None or t.bid <= 0 or t.ask <= 0: return None
        return Quote(t.bid, t.ask, self._utc(t.time), "mt5")

    def now(self) -> datetime: return datetime.now(timezone.utc)


class MT5Broker(Broker):
    name = "mt5"
    MAGIC = 424242

    def __init__(self, mt5, spec: InstrumentSpec, deviation: int = 10, allow_live: bool = False):
        self.mt5, self.spec, self.deviation = mt5, spec, deviation
        self._lock = threading.Lock()
        ai, ti = mt5.account_info(), mt5.terminal_info()
        if ai is None or ti is None: raise RuntimeError("MT5 account/terminal info unavailable")
        if not ai.trade_allowed or not ti.trade_allowed: raise RuntimeError("trading not allowed (enable Algo Trading in terminal / account)")
        real = ai.trade_mode == getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
        if real and not (allow_live and os.environ.get("APEX_I_UNDERSTAND_LIVE_RISK") == "yes"):
            raise RuntimeError("REAL account detected: set allow_live=true AND env APEX_I_UNDERSTAND_LIVE_RISK=yes. Test on demo first.")
        self.currency, self.real = ai.currency, real
        log.warning("MT5 broker ready: %s account", "REAL" if real else "demo/contest")

    def _filling(self, symbol):
        m = self.mt5; fm = self.mt5.symbol_info(symbol).filling_mode
        if fm & 2: return m.ORDER_FILLING_IOC
        if fm & 1: return m.ORDER_FILLING_FOK
        return m.ORDER_FILLING_RETURN

    def _mine(self, symbol=None):
        ps = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        return [p for p in (ps or []) if p.magic == self.MAGIC]

    def submit(self, req: OrderRequest, quote: Quote) -> OrderResult:
        m = self.mt5
        errs = req.validate(self.spec)
        if errs: return OrderResult("REJECTED", req.client_id, detail="; ".join(errs))
        if req.order_type != "market": return OrderResult("REJECTED", req.client_id, detail="live adapter supports market orders only")
        with self._lock:
            tag = req.client_id[:20]
            dup = [p for p in self._mine(req.symbol) if p.comment == tag]
            if dup: return OrderResult("DUPLICATE", req.client_id, str(dup[0].ticket))
            m.symbol_select(req.symbol, True)
            info, tick = m.symbol_info(req.symbol), m.symbol_info_tick(req.symbol)
            if tick is None or info is None: return OrderResult("REJECTED", req.client_id, detail="no tick/symbol info")
            buy = req.side == "BUY"; px = tick.ask if buy else tick.bid
            min_dist = info.trade_stops_level * info.point
            if abs(px - req.stop_loss) < min_dist or abs(req.take_profit - px) < min_dist:
                return OrderResult("REJECTED", req.client_id, detail=f"SL/TP closer than broker stops level ({min_dist})")
            request = {"action": m.TRADE_ACTION_DEAL, "symbol": req.symbol, "volume": req.size,
                       "type": m.ORDER_TYPE_BUY if buy else m.ORDER_TYPE_SELL, "price": px, "sl": req.stop_loss, "tp": req.take_profit,
                       "deviation": self.deviation, "magic": self.MAGIC, "comment": tag, "type_time": m.ORDER_TIME_GTC,
                       "type_filling": self._filling(req.symbol)}
            chk = m.order_check(request)
            if chk is None or chk.retcode != 0: return OrderResult("REJECTED", req.client_id, detail=f"order_check failed: {getattr(chk,'retcode',None)} {getattr(chk,'comment','')}")
            r = m.order_send(request)
            # never trust the return alone: query broker state, verify the position AND its protective orders
            time.sleep(0.2)
            pos = [p for p in self._mine(req.symbol) if p.comment == tag]
            if pos:
                p = pos[0]
                prot = abs(p.sl - req.stop_loss) <= self.spec.tick_size and abs(p.tp - req.take_profit) <= self.spec.tick_size
                if not prot:   # position open but protection wrong/missing: try to repair, else report loudly
                    fix = m.order_send({"action": m.TRADE_ACTION_SLTP, "symbol": req.symbol, "position": p.ticket, "sl": req.stop_loss, "tp": req.take_profit})
                    p2 = [q for q in self._mine(req.symbol) if q.ticket == p.ticket]
                    prot = bool(p2) and abs(p2[0].sl - req.stop_loss) <= self.spec.tick_size
                return OrderResult("FILLED" if prot else "UNKNOWN", req.client_id, str(p.ticket), p.price_open,
                                   "" if prot else "SL/TP could not be verified on broker: manual intervention required")
            if r is not None and r.retcode in (m.TRADE_RETCODE_REJECT, m.TRADE_RETCODE_INVALID, m.TRADE_RETCODE_NO_MONEY,
                                               m.TRADE_RETCODE_INVALID_VOLUME, m.TRADE_RETCODE_INVALID_STOPS, m.TRADE_RETCODE_MARKET_CLOSED):
                return OrderResult("REJECTED", req.client_id, detail=f"retcode={r.retcode}")
            return OrderResult("UNKNOWN", req.client_id, detail=f"ambiguous: retcode={getattr(r,'retcode',None)}, no position found")

    def lookup(self, client_id: str):
        for p in self._mine():
            if p.comment == client_id[:20]: return OrderResult("FILLED", client_id, str(p.ticket), p.price_open)
        return None

    def positions(self):
        out = []
        for p in self._mine():
            vpu = self.spec.value_per_price_unit
            out.append(dict(id=str(p.ticket), symbol=p.symbol, side="BUY" if p.type == 0 else "SELL", size=p.volume,
                            entry=p.price_open, sl=p.sl, tp=p.tp, risk=abs(p.price_open - p.sl) * vpu * p.volume if p.sl else 0.0))
        return out

    def account(self):
        a = self.mt5.account_info()
        return None if a is None else {"equity": a.equity, "balance": a.balance, "free_margin": a.margin_free, "currency": a.currency}

    def close_all(self, quote=None):
        m = self.mt5
        for p in self._mine():
            tick = m.symbol_info_tick(p.symbol); buy = p.type == 0
            r = m.order_send({"action": m.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume, "position": p.ticket,
                              "type": m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY, "price": tick.bid if buy else tick.ask,
                              "deviation": self.deviation * 3, "magic": self.MAGIC, "comment": "apex-close",
                              "type_time": m.ORDER_TIME_GTC, "type_filling": self._filling(p.symbol)})
            if r is None or r.retcode != m.TRADE_RETCODE_DONE: log.error("close failed for %s: %s", p.ticket, getattr(r, "retcode", None))

    def closed_since(self, ts: datetime):
        m = self.mt5
        deals = m.history_deals_get(datetime.fromtimestamp(ts.timestamp()), datetime.now()) or []
        return [{"pnl": d.profit + d.commission + d.swap, "ts": datetime.fromtimestamp(d.time, tz=timezone.utc)}
                for d in deals if d.magic == self.MAGIC and d.entry == getattr(m, "DEAL_ENTRY_OUT", 1)]

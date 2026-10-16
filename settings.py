"""JSON settings with strict validation. Secrets (passwords/tokens) come from environment variables only."""
from __future__ import annotations
import json
from dataclasses import dataclass, field, fields
from typing import Optional
from .config import RiskConfig, ExecConfig, EventPolicy, Mode, validate_config
from .scanner import ScanConfig


@dataclass
class FeedSettings:
    type: str = "csv"                 # csv | mt5 | oanda
    path: str = ""
    symbol: str = "EURUSD"
    tf_minutes: int = 15
    spread: float = 0.0001            # csv simulation only
    server_utc_offset_hours: Optional[float] = None
    oanda_account: str = ""
    oanda_practice: bool = True


@dataclass
class BrokerSettings:
    type: str = "paper"               # paper | mt5
    mt5_login: Optional[int] = None
    mt5_server: Optional[str] = None
    mt5_path: Optional[str] = None
    allow_live: bool = False
    commission_per_lot: float = 0.0
    slippage: float = 0.0


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    proposal_ttl_bars: int = 3
    max_drift_atr: float = 0.25
    state_path: str = "apex_state.json"
    journal_path: str = "apex_journal.jsonl"
    log_path: str = "apex.log"
    edge_path: str = "apex_edge.json"
    events_path: str = ""


@dataclass
class Settings:
    risk: RiskConfig = field(default_factory=RiskConfig)
    exec: ExecConfig = field(default_factory=ExecConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    feed: FeedSettings = field(default_factory=FeedSettings)
    broker: BrokerSettings = field(default_factory=BrokerSettings)
    server: ServerSettings = field(default_factory=ServerSettings)


def _build(cls, d: dict, name: str):
    allowed = {f.name for f in fields(cls)}
    bad = set(d) - allowed
    if bad: raise ValueError(f"unknown keys in '{name}': {sorted(bad)}")
    return cls(**d)


def load_settings(path: Optional[str]) -> Settings:
    d = {}
    if path:
        with open(path) as f: d = json.load(f)
    top = {"risk", "exec", "scan", "feed", "broker", "server"}
    if set(d) - top: raise ValueError(f"unknown top-level keys: {sorted(set(d) - top)}")
    s = Settings(_build(RiskConfig, d.get("risk", {}), "risk"), ExecConfig(), _build(ScanConfig, d.get("scan", {}), "scan"),
                 _build(FeedSettings, d.get("feed", {}), "feed"), _build(BrokerSettings, d.get("broker", {}), "broker"),
                 _build(ServerSettings, d.get("server", {}), "server"))
    e = dict(d.get("exec", {}))
    if "mode" in e: e["mode"] = Mode(e["mode"])
    if "allowed_sessions_utc" in e: e["allowed_sessions_utc"] = [tuple(x) for x in e["allowed_sessions_utc"]]
    s.exec = _build(ExecConfig, e, "exec")
    probs = validate_config(s.risk)
    if s.exec.mode == Mode.AUTONOMOUS: probs.append("autonomous mode cannot be enabled via settings")
    if s.feed.type not in ("csv", "mt5", "oanda"): probs.append("feed.type must be csv|mt5|oanda")
    if s.broker.type not in ("paper", "mt5"): probs.append("broker.type must be paper|mt5")
    if s.broker.type == "mt5" and s.feed.type == "csv": probs.append("live broker with CSV feed is not allowed")
    if s.feed.tf_minutes * 60 not in (60, 300, 900, 1800, 3600, 14400, 86400): probs.append("unsupported tf_minutes")
    if probs: raise ValueError("invalid settings: " + "; ".join(probs))
    return s

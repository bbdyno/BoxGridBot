"""SQLite 저장소. 표준 라이브러리만 쓰고 WAL 모드로 연다."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .events import Event
from .models import Fill, Trade

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
  symbol TEXT, tf TEXT, ts TEXT, o REAL, h REAL, l REAL, c REAL, v REAL,
  PRIMARY KEY(symbol, tf, ts));
CREATE TABLE IF NOT EXISTS fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT,
  qty REAL, price REAL, cost REAL, fee REAL, mode TEXT, strategy TEXT,
  reason TEXT, order_id TEXT, level INTEGER);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, strategy TEXT,
  entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL,
  qty REAL, pnl REAL, pnl_pct REAL, fee REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, severity TEXT,
  title TEXT, body TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    """체결·거래·이벤트·상태를 SQLite 에 남긴다."""

    def __init__(self, path: str = "data/boxgrid.db") -> None:
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as exc:  # pragma: no cover
            log.warning("WAL 모드를 켜지 못했습니다: %s", exc)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        """연결을 닫는다."""
        self.conn.close()

    # ---------- 기록 ----------
    def record_fill(self, fill: Fill) -> None:
        """체결 한 건을 남긴다."""
        self.conn.execute(
            "INSERT INTO fills(ts,symbol,side,qty,price,cost,fee,mode,strategy,reason,order_id,level)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (fill.ts.isoformat(), fill.symbol, fill.side, fill.qty, fill.price, fill.cost,
             fill.fee, fill.mode, fill.strategy, fill.reason, fill.order_id, fill.level),
        )
        self.conn.commit()

    def record_trade(self, trade: Trade) -> None:
        """청산된 거래를 남긴다."""
        self.conn.execute(
            "INSERT INTO trades(symbol,strategy,entry_time,exit_time,entry_price,exit_price,"
            "qty,pnl,pnl_pct,fee,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (trade.symbol, trade.strategy, trade.entry_time.isoformat(), trade.exit_time.isoformat(),
             trade.entry_price, trade.exit_price, trade.qty, trade.pnl, trade.pnl_pct,
             trade.fee, trade.exit_reason),
        )
        self.conn.commit()

    def record_event(self, event: Event) -> None:
        """알림 이벤트를 남긴다."""
        self.conn.execute(
            "INSERT INTO events(ts,kind,severity,title,body,data) VALUES(?,?,?,?,?,?)",
            (event.ts.isoformat(), event.kind.value, event.severity, event.title,
             event.body, json.dumps(event.data, ensure_ascii=False, default=str)),
        )
        self.conn.commit()

    def record_candles(self, symbol: str, tf: str, rows: list[tuple]) -> None:
        """캔들을 캐시한다. rows = [(ts_iso, o, h, l, c, v), ...]."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO candles(symbol,tf,ts,o,h,l,c,v) VALUES(?,?,?,?,?,?,?,?)",
            [(symbol, tf, *r) for r in rows],
        )
        self.conn.commit()

    # ---------- 조회 ----------
    def get_daily_pnl(self, date: str | None = None) -> float:
        """KST 기준 하루 실현 손익 합계."""
        date = date or datetime.now(KST).strftime("%Y-%m-%d")
        cur = self.conn.execute("SELECT exit_time, pnl FROM trades")
        total = 0.0
        for row in cur.fetchall():
            try:
                ts = datetime.fromisoformat(row["exit_time"])
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.astimezone(KST).strftime("%Y-%m-%d") == date:
                total += float(row["pnl"])
        return total

    def recent_events(self, limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
        """최근 이벤트(최신순)."""
        if kind:
            cur = self.conn.execute("SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        else:
            cur = self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def recent_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def recent_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        """최근 거래."""
        cur = self.conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def set_state(self, key: str, value: Any) -> None:
        """상태 값을 저장한다(JSON 직렬화)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO state(key,value) VALUES(?,?)",
            (key, json.dumps(value, ensure_ascii=False, default=str)),
        )
        self.conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        """상태 값을 읽는다."""
        cur = self.conn.execute("SELECT value FROM state WHERE key=?", (key,))
        row = cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

"""
燃气数据本地持久化存储模块。

功能概述：
1. 将 API 返回的日用气、月账单、月用气等数据写入 SQLite 数据库
2. 按保留规则自动清理过期数据（日数据 1 年、月数据 24 个月、年数据 3 年）
3. 提供缓存读取接口，供传感器优先使用本地缓存，补齐服务器返回不足的数据
4. 支持阶梯计费所需的年度用气量汇总

数据库表结构：
- daily_usage: 日用量（day, usage, cost, raw）
- monthly_bill: 月账单（month, amount, raw）
- monthly_usage: 月用气量（month, usage, raw）
- annual_bill: 年度账单汇总（year, amount）
- annual_usage: 年度用气汇总（year, usage）
- maintenance: 维护信息（如上次 VACUUM 日期）
"""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import timedelta, datetime
from typing import Any, Dict, List

from homeassistant.util import dt as dt_util

from .const import (
    CONF_FIXED_PRICE,
    CONF_PRICING_MODE,
    CONF_TIER_1_LIMIT,
    CONF_TIER_1_PRICE,
    CONF_TIER_2_LIMIT,
    CONF_TIER_2_PRICE,
    CONF_TIER_3_PRICE,
    DEFAULT_FIXED_PRICE,
    DEFAULT_PRICING_MODE,
    DEFAULT_TIER_1_LIMIT,
    DEFAULT_TIER_1_PRICE,
    DEFAULT_TIER_2_LIMIT,
    DEFAULT_TIER_2_PRICE,
    DEFAULT_TIER_3_PRICE,
    PRICING_MODE_FIXED,
)


class XjGasStorage:
    """
    燃气数据本地存储类。

    使用 SQLite 持久化日/月/年数据，并通过内存缓存加速读取。
    每次 persist 后刷新缓存，传感器优先使用缓存数据。
    """

    def __init__(self, hass, config_entry):
        """
        初始化存储实例。

        Args:
            hass: Home Assistant 实例
            config_entry: 配置条目，用于获取 entry_id 和计费选项
        """
        self._hass = hass
        self._config_entry = config_entry
        # 数据库路径：HA 的 .storage 目录，按 entry_id 区分不同账号
        self._db_path = hass.config.path(".storage", f"xjgas_{config_entry.entry_id}.sqlite")
        # 运行期缓存，减少反复读取数据库
        self._cache: Dict[str, Any] = {
            "daily": [],           # 日用气记录列表
            "monthly_bill": [],    # 月账单列表
            "monthly_usage": [],   # 月用气量列表
            "annual_bill": {},     # 年度账单汇总 {year: amount}
            "annual_usage": {},    # 年度用气汇总 {year: usage}
        }

    async def persist(self, data: Dict[str, Any]) -> None:
        """
        异步持久化数据到 SQLite 并刷新缓存。

        Args:
            data: 协调器汇总的数据，需包含 daily_usage、fee_record、meter_info 等键
        """
        if not data:
            return
        try:
            result = await self._hass.async_add_executor_job(self._persist_sync, data)
            if result:
                self._cache = result
        except Exception:
            pass

    @property
    def daily_records(self) -> List[Dict[str, Any]]:
        return self._cache.get("daily", [])

    @property
    def monthly_bills(self) -> List[Dict[str, Any]]:
        return self._cache.get("monthly_bill", [])

    @property
    def monthly_usage(self) -> List[Dict[str, Any]]:
        return self._cache.get("monthly_usage", [])

    @property
    def annual_bills(self) -> Dict[str, float]:
        return self._cache.get("annual_bill", {})

    @property
    def annual_usage(self) -> Dict[str, float]:
        return self._cache.get("annual_usage", {})

    def _init_db(self) -> None:
        """初始化数据库：创建表结构，启用 WAL 模式以减少锁冲突。"""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS daily_usage (day TEXT PRIMARY KEY, usage REAL, cost REAL, raw TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS monthly_bill (month TEXT PRIMARY KEY, amount REAL, raw TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS monthly_usage (month TEXT PRIMARY KEY, usage REAL, raw TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS annual_bill (year TEXT PRIMARY KEY, amount REAL)")
            conn.execute("CREATE TABLE IF NOT EXISTS annual_usage (year TEXT PRIMARY KEY, usage REAL)")
            conn.execute("CREATE TABLE IF NOT EXISTS maintenance (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
        finally:
            conn.close()

    def _persist_sync(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        同步持久化数据到 SQLite 数据库。

        将协调器汇总的 API 数据（日用气、月账单、月用气等）写入对应表，
        按保留规则清理过期数据，计算年度汇总，执行 VACUUM 维护，最后回填内存缓存。

        Args:
            data: 协调器汇总的数据字典，需包含 daily_usage、fee_record、meter_info 等键。

        Returns:
            dict: 持久化后的缓存数据，包含 daily、monthly_bill、monthly_usage、annual_bill、annual_usage。
        """
        # 初始化数据库并建立连接
        self._init_db()
        conn = sqlite3.connect(self._db_path)
        try:
            # === 日用气量与日账单 ===
            daily_rows = self._extract_rows(data.get("daily_usage"))
            unit_price = self._current_unit_price(data)
            daily_records = []
            for row in daily_rows:
                day = self._day_key_from_row(row)
                if not day:
                    continue
                # 统一解析日用量并计算日账单
                usage = self._extract_number(row, ["useGas", "gasVolume", "dailyGas", "usage", "gas", "num", "sl", "amount"])
                cost = round(usage * unit_price, 2) if unit_price > 0 and usage > 0 else 0.0
                raw = json.dumps(row, ensure_ascii=False, default=str)
                daily_records.append((day, usage, cost, raw))
            if daily_records:
                # 同一天记录进行替换更新
                conn.executemany(
                    "INSERT INTO daily_usage(day, usage, cost, raw) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET usage=excluded.usage, cost=excluded.cost, raw=excluded.raw",
                    daily_records,
                )
            # 仅保留近一年的日数据
            cutoff_day = (dt_util.now().date() - timedelta(days=366)).strftime("%Y-%m-%d")
            conn.execute("DELETE FROM daily_usage WHERE day < ?", (cutoff_day,))

            # === 月账单 ===
            monthly_rows = self._extract_rows(data.get("fee_record"))
            monthly_bill_records = []
            for row in monthly_rows:
                month = self._month_key_from_row(row)
                if not month:
                    continue
                # 解析月账单金额并补充 month 字段，方便前端使用
                amount = self._extract_number(row, ["paidInGasFee", "payableGasFee", "rcvblamt", "money", "totalFee", "payMoney", "fee", "billAmount", "rcvedamt", "amt", "gasFee", "pay"])
                row_with_month = dict(row)
                row_with_month["month"] = month
                raw = json.dumps(row_with_month, ensure_ascii=False, default=str)
                monthly_bill_records.append((month, amount, raw))
            if monthly_bill_records:
                # 同月账单进行替换更新
                conn.executemany(
                    "INSERT INTO monthly_bill(month, amount, raw) VALUES(?, ?, ?) "
                    "ON CONFLICT(month) DO UPDATE SET amount=excluded.amount, raw=excluded.raw",
                    monthly_bill_records,
                )

            # === 月用气量 ===
            usage_rows = self._extract_rows(data.get("meter_info"))
            monthly_usage_records = []
            for row in usage_rows:
                month = self._month_key_from_row(row)
                if not month:
                    continue
                # 解析月用气量并补充 month 字段
                usage = self._extract_number(row, ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas", "usage", "gas", "num", "sl", "amount"])
                row_with_month = dict(row)
                row_with_month["month"] = month
                raw = json.dumps(row_with_month, ensure_ascii=False, default=str)
                monthly_usage_records.append((month, usage, raw))
            if monthly_usage_records:
                # 同月用气量进行替换更新
                conn.executemany(
                    "INSERT INTO monthly_usage(month, usage, raw) VALUES(?, ?, ?) "
                    "ON CONFLICT(month) DO UPDATE SET usage=excluded.usage, raw=excluded.raw",
                    monthly_usage_records,
                )

            # 计算近 24 个月的起始月份
            current = dt_util.now().date()
            total = current.year * 12 + (current.month - 1)
            start_total = total - 23
            start_year = start_total // 12
            start_month = start_total % 12 + 1
            start_month_str = f"{start_year:04d}-{start_month:02d}"
            # 仅保留近 24 个月的月账单与月用气
            conn.execute("DELETE FROM monthly_bill WHERE month < ?", (start_month_str,))
            conn.execute("DELETE FROM monthly_usage WHERE month < ?", (start_month_str,))

            # === 年度汇总 ===
            current_year = current.year
            # 近 3 年（含当年）年度账单与年度用气
            annual_years = [current_year - 2, current_year - 1, current_year]
            for year in annual_years:
                prefix = f"{year:04d}-"
                # 按月份累计出年账单
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM monthly_bill WHERE month LIKE ?",
                    (prefix + "%",),
                ).fetchone()
                total_amount = float(row[0]) if row and row[0] is not None else 0.0
                conn.execute(
                    "INSERT INTO annual_bill(year, amount) VALUES(?, ?) "
                    "ON CONFLICT(year) DO UPDATE SET amount=excluded.amount",
                    (str(year), total_amount),
                )

                # 按月份累计出年用气
                row = conn.execute(
                    "SELECT COALESCE(SUM(usage), 0) FROM monthly_usage WHERE month LIKE ?",
                    (prefix + "%",),
                ).fetchone()
                total_usage = float(row[0]) if row and row[0] is not None else 0.0
                conn.execute(
                    "INSERT INTO annual_usage(year, usage) VALUES(?, ?) "
                    "ON CONFLICT(year) DO UPDATE SET usage=excluded.usage",
                    (str(year), total_usage),
                )

            # 清理超过 3 年的年度数据
            conn.execute(
                "DELETE FROM annual_bill WHERE year NOT IN (?, ?, ?)",
                (str(current_year - 2), str(current_year - 1), str(current_year)),
            )
            conn.execute(
                "DELETE FROM annual_usage WHERE year NOT IN (?, ?, ?)",
                (str(current_year - 2), str(current_year - 1), str(current_year)),
            )
            conn.commit()

            # === 数据库维护 ===
            # 通过记录上次 VACUUM 日期，避免每次写入都执行整理
            last_vacuum_row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                ("last_vacuum",),
            ).fetchone()
            should_vacuum = True
            if last_vacuum_row and last_vacuum_row[0]:
                try:
                    # 解析上次维护日期，默认每 30 天执行一次
                    last_vacuum_date = datetime.strptime(last_vacuum_row[0], "%Y-%m-%d").date()
                    should_vacuum = (dt_util.now().date() - last_vacuum_date).days >= 30
                except ValueError:
                    # 日期格式异常时直接触发维护
                    should_vacuum = True
            if should_vacuum:
                # 执行 VACUUM 释放空间并压缩数据库
                conn.execute("VACUUM")
                conn.execute(
                    "INSERT INTO maintenance(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("last_vacuum", dt_util.now().date().strftime("%Y-%m-%d")),
                )
                conn.commit()

            # === 读取数据库，回填缓存 ===
            daily_cached = []
            rows = conn.execute(
                "SELECT day, usage, cost, raw FROM daily_usage WHERE day >= ? ORDER BY day DESC",
                (cutoff_day,),
            ).fetchall()
            for day, usage, cost, raw in rows:
                raw_dict = None
                if raw:
                    try:
                        raw_dict = json.loads(raw)
                    except Exception:
                        raw_dict = None
                daily_cached.append({"day": day, "usage": usage, "cost": cost, "raw": raw_dict})

            monthly_bill_cached = []
            rows = conn.execute(
                "SELECT month, amount, raw FROM monthly_bill WHERE month >= ? ORDER BY month DESC",
                (start_month_str,),
            ).fetchall()
            for month, amount, raw in rows:
                raw_dict = None
                if raw:
                    try:
                        raw_dict = json.loads(raw)
                    except Exception:
                        raw_dict = None
                monthly_bill_cached.append({"month": month, "amount": amount, "raw": raw_dict})

            monthly_usage_cached = []
            rows = conn.execute(
                "SELECT month, usage, raw FROM monthly_usage WHERE month >= ? ORDER BY month DESC",
                (start_month_str,),
            ).fetchall()
            for month, usage, raw in rows:
                raw_dict = None
                if raw:
                    try:
                        raw_dict = json.loads(raw)
                    except Exception:
                        raw_dict = None
                monthly_usage_cached.append({"month": month, "usage": usage, "raw": raw_dict})

            annual_bill_cached = {}
            rows = conn.execute("SELECT year, amount FROM annual_bill").fetchall()
            for year, amount in rows:
                # 统一转为字符串键，便于与年份字符串对齐
                annual_bill_cached[str(year)] = float(amount)

            annual_usage_cached = {}
            rows = conn.execute("SELECT year, usage FROM annual_usage").fetchall()
            for year, usage in rows:
                # 统一转为字符串键，便于与年份字符串对齐
                annual_usage_cached[str(year)] = float(usage)

            return {
                "daily": daily_cached,
                "monthly_bill": monthly_bill_cached,
                "monthly_usage": monthly_usage_cached,
                "annual_bill": annual_bill_cached,
                "annual_usage": annual_usage_cached,
            }
        finally:
            conn.close()

    def _extract_rows(self, payload: Any) -> List[Dict[str, Any]]:
        """
        从接口返回中提取记录列表，兼容 rows/obj/data 等多种嵌套结构。
        """
        if not payload:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            rows = payload.get("rows")
            if isinstance(rows, list):
                return rows
            obj = payload.get("obj")
            if isinstance(obj, list):
                return obj
            if isinstance(obj, dict):
                rows = obj.get("result") or obj.get("rows")
                if isinstance(rows, list):
                    return rows
            data = payload.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                rows = data.get("rows")
                if isinstance(rows, list):
                    return rows
        return []

    def _normalize_day(self, value: Any) -> str:
        """统一日期为 YYYY-MM-DD 格式，兼容数字、字符串及多种分隔符。"""
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            value = str(int(value))
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if " " in value:
            value = value.split(" ", 1)[0]
        if "T" in value:
            value = value.split("T", 1)[0]
        value = value.replace("/", "-").replace(".", "-")
        digits = value.replace("-", "")
        if len(digits) == 8 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
        parts = [p for p in value.split("-") if p]
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            year = parts[0].zfill(4)
            month = parts[1].zfill(2)
            day = parts[2].zfill(2)
            if len(year) == 4:
                return f"{year}-{month}-{day}"
        return value

    def _day_key_from_row(self, row: Dict[str, Any]) -> str:
        """从记录行中提取日期字段，取多字段中最大的作为排序键。"""
        candidates = []
        for key in ["gasDay", "day", "date", "rq", "readDate", "chargeDate", "payDate", "time"]:
            if key in row:
                normalized = self._normalize_day(row.get(key))
                if normalized:
                    candidates.append(normalized)
        if not candidates:
            return ""
        return max(candidates)

    def _normalize_month(self, value: Any) -> str:
        """统一月份为 YYYY-MM 格式，兼容 202401、2024-01、2024.01 等。"""
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            value = str(int(value))
        if not isinstance(value, str):
            return ""
        value = value.strip().replace("/", "-").replace(".", "-")
        digits = value.replace("-", "")
        if len(digits) == 6 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}"
        if len(digits) == 8 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}"
        return value

    def _month_key_from_row(self, row: Dict[str, Any]) -> str:
        """从记录行中提取月份字段，兼容 readYm/month/billYm 等多种命名。"""
        month_value = (
            row.get("readYm")
            or row.get("month")
            or row.get("billYm")
            or row.get("ym")
            or row.get("billMonth")
            or row.get("chargeYm")
            or row.get("date")
            or row.get("rq")
        )
        return self._normalize_month(month_value)

    def _extract_number(self, row: Dict[str, Any], keys: List[str]) -> float:
        """按候选键顺序提取数值，失败返回 0.0。"""
        for k in keys:
            if k in row and row[k] is not None:
                try:
                    return float(row[k])
                except (ValueError, TypeError):
                    continue
        return 0.0

    def _current_unit_price(self, data: Dict[str, Any]) -> float:
        """根据计费模式与年度用气量计算当前单价。"""
        options = self._config_entry.options
        pricing_mode = options.get(CONF_PRICING_MODE, DEFAULT_PRICING_MODE)
        if pricing_mode == PRICING_MODE_FIXED:
            return options.get(CONF_FIXED_PRICE, DEFAULT_FIXED_PRICE)
        tier_1_limit = options.get(CONF_TIER_1_LIMIT, DEFAULT_TIER_1_LIMIT)
        tier_2_limit = options.get(CONF_TIER_2_LIMIT, DEFAULT_TIER_2_LIMIT)
        tier_1_price = options.get(CONF_TIER_1_PRICE, DEFAULT_TIER_1_PRICE)
        tier_2_price = options.get(CONF_TIER_2_PRICE, DEFAULT_TIER_2_PRICE)
        tier_3_price = options.get(CONF_TIER_3_PRICE, DEFAULT_TIER_3_PRICE)
        yearly_usage = self._yearly_usage_from_rows(self._extract_rows(data.get("meter_info")))
        if yearly_usage <= tier_1_limit:
            return tier_1_price
        if yearly_usage <= tier_2_limit:
            return tier_2_price
        return tier_3_price

    def _yearly_usage_from_rows(self, rows: List[Dict[str, Any]]) -> float:
        """累计当前年度月用气量，用于阶梯计价判断。"""
        total_usage = 0.0
        current_year = str(dt_util.now().year)
        for row in rows:
            month_key = self._month_key_from_row(row)
            if not month_key or not month_key.startswith(current_year):
                continue
            usage = self._extract_number(row, ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas"])
            total_usage += usage
        return total_usage

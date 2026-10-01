"""通用工具：时间、日期、期限判断。"""
from datetime import date, datetime, timedelta

FMT = "%Y-%m-%d %H:%M:%S"


def now() -> str:
    return datetime.now().strftime(FMT)


def today() -> date:
    return date.today()


def parse_delivery_date(text: str) -> date:
    """配送单 delivered_at 形如 '2026-09-01 09:00:00'。"""
    return datetime.strptime(text, FMT).date()


def days_between(d0: str, d1: date = None) -> int:
    """从 d0（字符串）到 d1（默认今天）经过的整天数。"""
    return (d1 or today()) - parse_delivery_date(d0)


def within_return_window(delivered_at: str, window_days: int, ref: date = None) -> bool:
    """签收次日起 window_days 天内可退；当天也算未超期（<=window）。"""
    return days_between(delivered_at, ref) <= timedelta(days=window_days)

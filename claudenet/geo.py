"""出口 IP 归属地与时区解析。"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .net import Proxy, fetch_json, fetch_trace, http_request

# 少数国家/地区在接口缺少时区字段时兜底（多时区国家优先使用接口返回的城市时区）
COUNTRY_FALLBACK_TZ = {
    "US": "America/Los_Angeles",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "SG": "Asia/Singapore",
    "HK": "Asia/Hong_Kong",
    "TW": "Asia/Taipei",
    "GB": "Europe/London",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "NL": "Europe/Amsterdam",
    "CA": "America/Toronto",
    "AU": "Australia/Sydney",
    "IN": "Asia/Kolkata",
    "TR": "Europe/Istanbul",
    "AE": "Asia/Dubai",
    "BR": "America/Sao_Paulo",
    "RU": "Europe/Moscow",
    "TH": "Asia/Bangkok",
    "MY": "Asia/Kuala_Lumpur",
    "VN": "Asia/Ho_Chi_Minh",
    "PH": "Asia/Manila",
    "ID": "Asia/Jakarta",
}

CN_NAMES = {
    "US": "美国", "JP": "日本", "KR": "韩国", "SG": "新加坡", "HK": "中国香港",
    "TW": "中国台湾", "GB": "英国", "DE": "德国", "FR": "法国", "NL": "荷兰",
    "CA": "加拿大", "AU": "澳大利亚", "IN": "印度", "TR": "土耳其", "AE": "阿联酋",
    "BR": "巴西", "RU": "俄罗斯", "TH": "泰国", "MY": "马来西亚", "VN": "越南",
    "PH": "菲律宾", "ID": "印度尼西亚", "CN": "中国", "MO": "中国澳门",
}

# 常被 Anthropic 限制的地区（仅作提示，最终以接口实测为准）
BLOCKED_REGIONS = {
    "CN", "HK", "MO", "RU", "BY", "IR", "KP", "SY", "CU",
}


def flag_emoji(code: str) -> str:
    code = (code or "").upper()
    if len(code) != 2 or not code.isalpha():
        return "🌐"
    return chr(0x1F1E6 + ord(code[0]) - 65) + chr(0x1F1E6 + ord(code[1]) - 65)


def country_name(code: str, english: str = "") -> str:
    code = (code or "").upper()
    return CN_NAMES.get(code) or english or code or "未知"


@dataclass
class GeoInfo:
    ip: str = ""
    country_code: str = ""
    country: str = ""
    region: str = ""
    city: str = ""
    timezone: str = ""
    asn: str = ""
    org: str = ""
    isp: str = ""
    flag: str = ""
    colo: str = ""
    source: str = ""
    trace_ip: str = ""
    trace_loc: str = ""
    elapsed_ms: int = 0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def place(self) -> str:
        bits = [b for b in (self.city, self.region, self.country) if b]
        return " · ".join(bits)


def _pick_timezone(data: Dict[str, Any]) -> str:
    tz = data.get("timezone")
    if isinstance(tz, dict):
        return str(tz.get("id") or "")
    if isinstance(tz, str):
        return tz
    return ""


def _from_ipwho(data: Dict[str, Any]) -> GeoInfo:
    tz = _pick_timezone(data)
    code = str(data.get("country_code") or "")
    conn = data.get("connection") or {}
    return GeoInfo(
        ip=str(data.get("ip") or ""),
        country_code=code,
        country=str(data.get("country") or ""),
        region=str(data.get("region") or ""),
        city=str(data.get("city") or ""),
        timezone=tz,
        asn=str(conn.get("asn") or ""),
        org=str(conn.get("org") or ""),
        isp=str(conn.get("isp") or ""),
        flag=flag_emoji(code),
        source="ipwho.is",
    )


def _from_ipapi_is(data: Dict[str, Any], ip: str = "") -> GeoInfo:
    code = str(data.get("country_code") or "")
    return GeoInfo(
        ip=str(data.get("ip") or ip),
        country_code=code,
        country=str(data.get("country") or ""),
        region=str(data.get("region") or ""),
        city=str(data.get("city") or ""),
        timezone=str(data.get("timezone") or ""),
        asn=str(data.get("asn") or ""),
        org=str(data.get("company") or ""),
        flag=flag_emoji(code),
        source="api.ipapi.is",
    )


def _from_ipapi_co(data: Dict[str, Any]) -> GeoInfo:
    code = str(data.get("country_code") or data.get("country") or "")
    return GeoInfo(
        ip=str(data.get("ip") or ""),
        country_code=code if len(code) == 2 else "",
        country=str(data.get("country_name") or ""),
        region=str(data.get("region") or ""),
        city=str(data.get("city") or ""),
        timezone=str(data.get("timezone") or ""),
        asn=str(data.get("asn") or ""),
        org=str(data.get("org") or ""),
        flag=flag_emoji(code),
        source="ipapi.co",
    )


def fetch_geo(proxy: Optional[Proxy], timeout: float = 8.0) -> GeoInfo:
    """获取出口 IP 归属地。先用 Cloudflare trace 拿边缘视角，再补全城市/时区。"""
    started = time.monotonic()
    trace = fetch_trace(proxy, timeout=timeout)
    geo: Optional[GeoInfo] = None
    errors: List[str] = []

    data, err = fetch_json("https://ipwho.is/", proxy, timeout=timeout)
    if isinstance(data, dict) and data.get("success", True) and data.get("ip"):
        geo = _from_ipwho(data)
    else:
        errors.append(f"ipwho.is: {err or '无数据'}")

    if geo is None:
        ip = trace.get("ip", "")
        url = f"https://api.ipapi.is/?q={ip}" if ip else "https://api.ipapi.is/"
        data, err = fetch_json(url, proxy, timeout=timeout)
        if isinstance(data, dict) and data.get("ip"):
            geo = _from_ipapi_is(data, ip)
        else:
            errors.append(f"ipapi.is: {err or '无数据'}")

    if geo is None:
        data, err = fetch_json("https://ipapi.co/json/", proxy, timeout=timeout)
        if isinstance(data, dict) and data.get("ip"):
            geo = _from_ipapi_co(data)
        else:
            errors.append(f"ipapi.co: {err or '无数据'}")

    if geo is None:
        geo = GeoInfo(error="；".join(errors) or "无法获取出口信息")

    geo.trace_ip = trace.get("ip", "")
    geo.trace_loc = trace.get("loc", "")
    geo.colo = trace.get("colo", "")
    if not geo.ip:
        geo.ip = geo.trace_ip

    # 以 Cloudflare 的 loc 为准校正国家（这是 Claude 实际看到的判定依据）
    if trace.get("loc") and len(trace["loc"]) == 2:
        if not geo.country_code:
            geo.country_code = trace["loc"]
        elif geo.country_code != trace["loc"]:
            geo.flag = flag_emoji(trace["loc"])

    if not geo.timezone and geo.country_code:
        geo.timezone = COUNTRY_FALLBACK_TZ.get(geo.country_code, "")
    if not geo.flag:
        geo.flag = flag_emoji(geo.country_code)
    if not geo.country:
        geo.country = country_name(geo.country_code)
    geo.elapsed_ms = int((time.monotonic() - started) * 1000)
    return geo


def is_blocked_region(code: str) -> bool:
    return (code or "").upper() in BLOCKED_REGIONS

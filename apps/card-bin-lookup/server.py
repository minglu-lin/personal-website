#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Card BIN / IIN Lookup Microservice (全球卡 BIN 智能雷达系统)
- 智能分流架构 (Traffic Splitting Engine):
  * 6~8位标准 BIN: 优先走免费公共通道 (HandyAPI + Binlist)，100% 免费，零消耗 Neutrino 额度
  * 9~10位扩展子 BIN: 精准路由至 Neutrino API (支持 10 位高精度解析)
  * Neutrino 遇额度或故障时自动无感降级至免费 8 位通道
- 本地 SQLite 持久化多级缓存 (0ms 极速响应)
- 访问密码授权保护 (Access Password Gate)
- 暂时去除单 IP 60s 频控限制 (Rate limit temporarily disabled as requested)
"""

import os
import sys
import json
import time
import sqlite3
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from collections import defaultdict

# --- CONFIGURATION ---
HOST = "127.0.0.1"
PORT = 18895
ACCESS_PASSWORD = os.getenv("BIN_ACCESS_PASSWORD", "11231632")
AUTH_ENABLED = False  # 访问密码开关（按用户需求：暂时关闭密码，免密直接访问）
NEUTRINO_USER_ID = "wexnsk"
NEUTRINO_KEYS = [
    "oJol8jsfcMdyRqGKE77sJx8C0o8DlDjrrxdrLXoJkz6AoNJy",  # Production
    "hT5VxCBHo95xNXGt0NYyC5Q2tydsrRUMFWossIWIgiJES8e4",  # Testing
    "SmRrr5azD3KgKO8vHPYSaHLtIXbqFxeBUl5m6S89czvtXZ3L"   # Root fallback
]
NEUTRINO_API_URL = "https://neutrinoapi.net/bin-lookup"
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DATA_DIR, "bin_cache.db")

# 频率限制开关（按用户需求：暂时去除 60s 频控限制）
RATE_LIMIT_ENABLED = False
RATE_LIMIT_SECONDS = 60
MAX_REQUESTS_PER_WINDOW = 1
REQUEST_HISTORY = defaultdict(list)

# 国家与币种对照表
COUNTRY_CURRENCY = {
    "US": "USD", "CN": "CNY", "GB": "GBP", "JP": "JPY", "DE": "EUR",
    "FR": "EUR", "PL": "PLN", "CA": "CAD", "AU": "AUD", "SG": "SGD",
    "HK": "HKD", "TW": "TWD", "KR": "KRW", "ES": "EUR", "IT": "EUR",
    "NL": "EUR", "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK",
    "FI": "EUR", "IE": "EUR", "NZ": "NZD", "BR": "BRL", "MX": "MXN",
    "IN": "INR", "RU": "RUB", "ZA": "ZAR", "AE": "AED", "SA": "SAR",
    "MY": "MYR", "TH": "THB", "VN": "VND", "PH": "PHP", "ID": "IDR",
    "AT": "EUR", "BE": "EUR", "CZ": "CZK", "PT": "EUR", "GR": "EUR",
    "HU": "HUF", "RO": "RON", "TR": "TRY", "IL": "ILS", "AR": "ARS",
    "CL": "CLP", "CO": "COP", "PE": "PEN", "EG": "EGP", "NG": "NGN"
}

# Country code to Chinese Name mapping
COUNTRY_ZH = {
    "US": "美国", "CN": "中国", "GB": "英国", "JP": "日本", "DE": "德国",
    "FR": "法国", "PL": "波兰", "CA": "加拿大", "AU": "澳大利亚", "SG": "新加坡",
    "HK": "中国香港", "TW": "中国台湾", "KR": "韩国", "ES": "西班牙", "IT": "意大利",
    "NL": "荷兰", "CH": "瑞士", "SE": "瑞典", "NO": "挪威", "DK": "丹麦",
    "FI": "芬兰", "IE": "爱尔兰", "NZ": "新西兰", "BR": "巴西", "MX": "墨西哥",
    "IN": "印度", "RU": "俄罗斯", "ZA": "南非", "AE": "阿联酋", "SA": "沙特阿拉伯",
    "MY": "马来西亚", "TH": "泰国", "VN": "越南", "PH": "菲律宾", "ID": "印度尼西亚",
    "AT": "奥地利", "BE": "比利时", "CZ": "捷克", "PT": "葡萄牙", "GR": "希腊",
    "HU": "匈牙利", "RO": "罗马尼亚", "TR": "土耳其", "IL": "以色列", "AR": "阿根廷",
    "CL": "智利", "CO": "哥伦比亚", "PE": "秘鲁", "EG": "埃及", "NG": "尼日利亚"
}

# Country flag emojis
COUNTRY_FLAGS = {
    "US": "🇺🇸", "CN": "🇨🇳", "GB": "🇬🇧", "JP": "🇯🇵", "DE": "🇩🇪",
    "FR": "🇫🇷", "PL": "🇵🇱", "CA": "🇨🇦", "AU": "🇦🇺", "SG": "🇸🇬",
    "HK": "🇭🇰", "TW": "🇹🇼", "KR": "🇰🇷", "ES": "🇪🇸", "IT": "🇮🇹",
    "NL": "🇳🇱", "CH": "🇨🇭", "SE": "🇸🇪", "NO": "🇳🇴", "DK": "🇩🇰",
    "FI": "🇫🇮", "IE": "🇮🇪", "NZ": "🇳🇿", "BR": "🇧🇷", "MX": "🇲🇽",
    "IN": "🇮🇳", "RU": "🇷🇺", "ZA": "🇿🇦", "AE": "🇦🇪", "SA": "🇸🇦",
    "MY": "🇲🇾", "TH": "🇹🇭", "VN": "🇻🇳", "PH": "🇵🇭", "ID": "🇮🇩",
    "AT": "🇦🇹", "BE": "🇧🇪", "CZ": "🇨🇿", "PT": "🇵🇹", "GR": "🇬🇷"
}

# Card brand styling metadata
BRAND_META = {
    "VISA": {"name": "Visa", "theme": "visa", "icon": "visa", "gradient": "linear-gradient(135deg, #1a1f71 0%, #0056b3 100%)"},
    "MASTERCARD": {"name": "Mastercard", "theme": "mastercard", "icon": "mastercard", "gradient": "linear-gradient(135deg, #24292e 0%, #cf4324 100%)"},
    "AMERICAN EXPRESS": {"name": "American Express", "theme": "amex", "icon": "amex", "gradient": "linear-gradient(135deg, #0070d2 0%, #002f6c 100%)"},
    "AMEX": {"name": "American Express", "theme": "amex", "icon": "amex", "gradient": "linear-gradient(135deg, #0070d2 0%, #002f6c 100%)"},
    "DISCOVER": {"name": "Discover", "theme": "discover", "icon": "discover", "gradient": "linear-gradient(135deg, #ff6000 0%, #1a1a1a 100%)"},
    "JCB": {"name": "JCB", "theme": "jcb", "icon": "jcb", "gradient": "linear-gradient(135deg, #0e5c9e 0%, #2b9148 100%)"},
    "UNIONPAY": {"name": "UnionPay", "theme": "unionpay", "icon": "unionpay", "gradient": "linear-gradient(135deg, #004b79 0%, #bf1b24 100%)"},
    "CHINA UNIONPAY": {"name": "UnionPay", "theme": "unionpay", "icon": "unionpay", "gradient": "linear-gradient(135deg, #004b79 0%, #bf1b24 100%)"},
    "DINERS CLUB": {"name": "Diners Club", "theme": "diners", "icon": "diners", "gradient": "linear-gradient(135deg, #004a80 0%, #002b4d 100%)"},
    "MAESTRO": {"name": "Maestro", "theme": "maestro", "icon": "maestro", "gradient": "linear-gradient(135deg, #0056b3 0%, #d81e05 100%)"}
}

# Card Category translation
CATEGORY_ZH = {
    "CLASSIC": "经典卡 (Classic)",
    "STANDARD": "标准卡 (Standard)",
    "GOLD": "金卡 (Gold)",
    "PLATINUM": "白金卡 (Platinum)",
    "TITANIUM": "钛金卡 (Titanium)",
    "INFINITE": "无限卡 (Infinite)",
    "SIGNATURE": "御玺卡 (Signature)",
    "WORLD": "世界卡 (World)",
    "WORLD ELITE": "世界之极卡 (World Elite)",
    "BUSINESS": "商务卡 (Business)",
    "CORPORATE": "企业卡 (Corporate)",
    "COMMERCIAL": "商业卡 (Commercial)",
    "FLEET": "车队卡 (Fleet)",
    "PURCHASING": "采购卡 (Purchasing)",
    "PREMIER": "卓越卡 (Premier)",
    "REWARDS": "奖励卡 (Rewards)",
    "CENTURION": "百夫长黑金卡 (Centurion)"
}

TYPE_ZH = {
    "CREDIT": "信用卡 (Credit)",
    "DEBIT": "借记卡 (Debit)",
    "PREPAID": "预付费卡/虚拟卡 (Prepaid)",
    "CHARGE CARD": "签账卡 (Charge)"
}

# --- DATABASE & CACHE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bin_cache (
            bin_prefix TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            hit_count INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()

def get_from_cache(bin_code):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # Check exact match first
    cur.execute("SELECT data_json, hit_count FROM bin_cache WHERE bin_prefix = ?", (bin_code,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE bin_cache SET hit_count = hit_count + 1 WHERE bin_prefix = ?", (bin_code,))
        conn.commit()
        conn.close()
        res = json.loads(row[0])
        res["source_engine"] = "本地 SQLite 高速缓存"
        return res, True

    # Check prefix match from 10 down to 6 digits
    for length in range(min(len(bin_code), 10), 5, -1):
        prefix = bin_code[:length]
        cur.execute("SELECT data_json, hit_count FROM bin_cache WHERE bin_prefix = ?", (prefix,))
        r = cur.fetchone()
        if r:
            cur.execute("UPDATE bin_cache SET hit_count = hit_count + 1 WHERE bin_prefix = ?", (prefix,))
            conn.commit()
            conn.close()
            res = json.loads(r[0])
            res["source_engine"] = "本地 SQLite 模糊缓存"
            return res, True

    conn.close()
    return None, False

def save_to_cache(bin_code, data):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "INSERT OR REPLACE INTO bin_cache (bin_prefix, data_json, created_at, hit_count) VALUES (?, ?, ?, 1)",
        (bin_code, json.dumps(data), now)
    )
    returned_bin = data.get("bin-number")
    if returned_bin and returned_bin != bin_code:
        cur.execute(
            "INSERT OR REPLACE INTO bin_cache (bin_prefix, data_json, created_at, hit_count) VALUES (?, ?, ?, 1)",
            (returned_bin, json.dumps(data), now)
        )
    conn.commit()
    conn.close()

def get_cache_stats():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(hit_count) FROM bin_cache")
    row = cur.fetchone()
    conn.close()
    return {
        "cached_bins": row[0] or 0,
        "total_cache_hits": row[1] or 0
    }

# --- LUHN CHECK ---
def luhn_verify(card_number_str):
    """Verifies Luhn mod-10 algorithm for a numeric string."""
    digits = [int(c) for c in card_number_str if c.isdigit()]
    if len(digits) < 13:
        return None  # Only full numbers can be definitively validated
    checksum = 0
    reverse_digits = digits[::-1]
    for i, d in enumerate(reverse_digits):
        if i % 2 == 1:
            doubled = d * 2
            checksum += doubled if doubled < 10 else doubled - 9
        else:
            checksum += d
    return (checksum % 10) == 0

# --- FREE API 1: HandyAPI (6-8 DIGIT FREE ROUTER) ---
def query_handy_api(bin_code):
    """Query HandyAPI free public database for 6-8 digit BINs (No API key, free, fast)."""
    bin8 = bin_code[:8]
    url = f"https://data.handyapi.com/bin/{bin8}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("Status") == "SUCCESS":
                    c_code = (data.get("Country", {}).get("A2") or "").upper()
                    tier = (data.get("CardTier") or "STANDARD").upper()
                    c_type = (data.get("Type") or "CREDIT").upper()
                    scheme = (data.get("Scheme") or "UNKNOWN").upper()
                    return {
                        "country": (data.get("Country", {}).get("Name") or "").upper(),
                        "country-code": c_code,
                        "country-code3": (data.get("Country", {}).get("A3") or "").upper(),
                        "card-brand": scheme,
                        "card-type": c_type,
                        "card-category": tier,
                        "issuer": data.get("Issuer") or "未知发卡行 (Unknown)",
                        "issuer-website": "",
                        "issuer-phone": "",
                        "currency-code": COUNTRY_CURRENCY.get(c_code, ""),
                        "is-commercial": any(k in tier for k in ["BUSINESS", "CORPORATE", "COMMERCIAL", "FLEET"]),
                        "is-prepaid": "PREPAID" in c_type or "VIRTUAL" in tier,
                        "is-reloadable": True,
                        "valid": True,
                        "bin-number": bin8,
                        "source_engine": "HandyAPI (8位免费分流通道)"
                    }
    except Exception:
        pass
    return None

# --- FREE API 2: Binlist.net (FALLBACK) ---
def query_binlist_api(bin_code):
    """Query binlist.net free community API."""
    bin8 = bin_code[:8]
    url = f"https://lookup.binlist.net/{bin8}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept-Version": "3",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                c_code = (data.get("country", {}).get("alpha2") or "").upper()
                return {
                    "country": (data.get("country", {}).get("name") or "").upper(),
                    "country-code": c_code,
                    "country-code3": "",
                    "card-brand": (data.get("scheme") or "").upper(),
                    "card-type": (data.get("type") or "").upper(),
                    "card-category": (data.get("brand") or "").upper(),
                    "issuer": data.get("bank", {}).get("name", ""),
                    "issuer-website": data.get("bank", {}).get("url", ""),
                    "issuer-phone": data.get("bank", {}).get("phone", ""),
                    "currency-code": (data.get("country", {}).get("currency") or COUNTRY_CURRENCY.get(c_code, "")).upper(),
                    "is-commercial": False,
                    "is-prepaid": bool(data.get("prepaid", False)),
                    "is-reloadable": True,
                    "valid": True,
                    "bin-number": bin8,
                    "source_engine": "Binlist (8位免费备用通道)"
                }
    except Exception:
        pass
    return None

# --- HIGH-PRECISION API: Neutrino API (10-DIGIT SUB-BIN ROUTER) ---
def query_neutrino_api(bin_code):
    """Query Neutrino API for 10-digit sub-BIN precision."""
    data = urllib.parse.urlencode({"bin-number": bin_code}).encode("utf-8")
    
    last_err = ""
    for key in NEUTRINO_KEYS:
        req = urllib.request.Request(
            NEUTRINO_API_URL,
            data=data,
            headers={
                "User-ID": NEUTRINO_USER_ID,
                "API-Key": key,
                "User-Agent": "10DigitCardBinViewer/1.0",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    res_body = resp.read().decode("utf-8")
                    parsed = json.loads(res_body)
                    parsed["source_engine"] = "Neutrino API (10位高精通道)"
                    return parsed, None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            last_err = f"HTTP {e.code}: {err_body}"
            continue
        except Exception as e:
            last_err = str(e)
            continue

    return None, last_err

# --- HEURISTIC FALLBACK ---
def heuristic_detect(clean_bin):
    """Offline heuristic detection when external APIs are unreachable."""
    brand = "UNKNOWN"
    if clean_bin.startswith("4"):
        brand = "VISA"
    elif clean_bin.startswith(("51", "52", "53", "54", "55")) or (len(clean_bin) >= 4 and 2221 <= int(clean_bin[:4]) <= 2720):
        brand = "MASTERCARD"
    elif clean_bin.startswith(("34", "37")):
        brand = "AMERICAN EXPRESS"
    elif clean_bin.startswith(("6011", "65")) or (len(clean_bin) >= 3 and 644 <= int(clean_bin[:3]) <= 649):
        brand = "DISCOVER"
    elif clean_bin.startswith("35"):
        brand = "JCB"
    elif clean_bin.startswith("62"):
        brand = "UNIONPAY"
    elif clean_bin.startswith(("300", "305", "36", "38")):
        brand = "DINERS CLUB"

    return {
        "valid": brand != "UNKNOWN",
        "bin-number": clean_bin[:6],
        "card-brand": brand,
        "card-type": "CREDIT / DEBIT",
        "card-category": "STANDARD",
        "issuer": "离线规则匹配 (Offline)",
        "country": "UNKNOWN",
        "country-code": "",
        "country-code3": "",
        "currency-code": "",
        "is-commercial": False,
        "is-prepaid": False,
        "is-reloadable": False,
        "heuristic": True,
        "source_engine": "离线卡组织规则匹配"
    }

# --- SMART TRAFFIC SPLITTING DISPATCHER ---
def dispatch_bin_lookup(clean_bin):
    """
    智能分流引擎:
    1. 6~8位标准查询: 优先路由至免费 HandyAPI / Binlist 通道 (0 消耗 Neutrino 额度)
    2. 9~10位高精查询: 路由至 Neutrino API (支持 10 位 Sub-BIN 解析)
    3. 互相降级保障
    """
    query_len = len(clean_bin)
    
    # 策略 A: 9-10 位扩展查询 -> 优先 Neutrino 高精通道
    if query_len >= 9:
        query_10 = clean_bin[:10]
        data, err = query_neutrino_api(query_10)
        if data and data.get("valid", True):
            return data
        # Neutrino 故障或额度耗尽时，降级分流到 8 位免费通道
        handy = query_handy_api(clean_bin[:8])
        if handy:
            return handy
        binlist = query_binlist_api(clean_bin[:8])
        if binlist:
            return binlist

    # 策略 B: 6-8 位标准查询 -> 优先走免费公共 API 通道
    else:
        # 1. 尝试免费通道 HandyAPI
        handy = query_handy_api(clean_bin)
        if handy:
            return handy
        # 2. 尝试备用免费通道 Binlist
        binlist = query_binlist_api(clean_bin)
        if binlist:
            return binlist
        # 3. 免费通道未命中时，再用 Neutrino 兜底
        data, err = query_neutrino_api(clean_bin)
        if data and data.get("valid", True):
            return data

    # 策略 C: 离线启发式规则兜底
    return heuristic_detect(clean_bin)

# --- ENRICH RESPONSE ---
def enrich_result(clean_bin, raw_data, is_cached=False):
    brand = (raw_data.get("card-brand") or "UNKNOWN").upper()
    c_code = (raw_data.get("country-code") or "").upper()
    cat = (raw_data.get("card-category") or "").upper()
    c_type = (raw_data.get("card-type") or "").upper()

    b_meta = BRAND_META.get(brand, {
        "name": brand.title(),
        "theme": "generic",
        "icon": "credit-card",
        "gradient": "linear-gradient(135deg, #1e293b 0%, #0f172a 100%)"
    })

    return {
        "success": True,
        "cached": is_cached,
        "query_bin": clean_bin,
        "bin_length": len(clean_bin),
        "source_engine": raw_data.get("source_engine", "云端实时解析"),
        "data": {
            "valid": raw_data.get("valid", True),
            "bin_number": raw_data.get("bin-number", clean_bin[:6]),
            "card_brand": brand,
            "card_type": c_type,
            "card_category": cat,
            "issuer": raw_data.get("issuer") or "未知发卡机构 (Unknown Bank)",
            "issuer_website": raw_data.get("issuer-website", ""),
            "issuer_phone": raw_data.get("issuer-phone", ""),
            "country": raw_data.get("country", ""),
            "country_code": c_code,
            "country_code3": raw_data.get("country-code3", ""),
            "currency_code": raw_data.get("currency-code", ""),
            "is_commercial": bool(raw_data.get("is-commercial", False)),
            "is_prepaid": bool(raw_data.get("is-prepaid", False)),
            "is_reloadable": bool(raw_data.get("is-reloadable", False))
        },
        "meta": {
            "brand_display": b_meta["name"],
            "brand_theme": b_meta["theme"],
            "brand_gradient": b_meta["gradient"],
            "country_flag": COUNTRY_FLAGS.get(c_code, "🌐"),
            "flag_url": f"https://flagcdn.com/w160/{c_code.lower()}.png" if c_code and len(c_code) == 2 else "",
            "country_name_zh": COUNTRY_ZH.get(c_code, raw_data.get("country", "未知国家/地区")),
            "card_type_zh": TYPE_ZH.get(c_type, c_type or "通用卡"),
            "card_category_zh": CATEGORY_ZH.get(cat, cat or "标准级"),
            "is_commercial_zh": "商业/企业账户" if raw_data.get("is-commercial") else "个人消费者账户",
            "is_prepaid_zh": "虚拟卡 / 预付费卡" if raw_data.get("is-prepaid") else "实体标准账户",
            "is_reloadable_zh": "支持反复充值" if raw_data.get("is-reloadable") else "一次性 / 绑定账户",
            "luhn_valid": luhn_verify(clean_bin)
        }
    }

# --- HTTP REQUEST HANDLER ---
class BinRequestHandler(BaseHTTPRequestHandler):
    def check_auth(self, req_data=None):
        """Verifies access password from Header, Query, Bearer token, or JSON body."""
        if not AUTH_ENABLED:
            return True

        # 1. Header: X-Access-Password
        pwd = self.headers.get("X-Access-Password")
        if pwd and pwd.strip() == ACCESS_PASSWORD:
            return True

        # 2. Authorization: Bearer <password>
        auth_hdr = self.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
            if token == ACCESS_PASSWORD:
                return True

        # 3. Query Param: ?password=...
        parsed_url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_url.query)
        if query.get("password") and query.get("password")[0] == ACCESS_PASSWORD:
            return True

        # 4. JSON Body: {"password": "..."}
        if req_data and isinstance(req_data, dict):
            if req_data.get("password") == ACCESS_PASSWORD:
                return True

        return False

    def check_rate_limit(self, client_ip):
        """Rate limiter - currently disabled as requested."""
        if not RATE_LIMIT_ENABLED:
            return True, 0
        now = time.time()
        history = REQUEST_HISTORY[client_ip]
        valid_history = [t for t in history if now - t < RATE_LIMIT_SECONDS]
        REQUEST_HISTORY[client_ip] = valid_history
        if len(valid_history) >= MAX_REQUESTS_PER_WINDOW:
            wait_sec = int(RATE_LIMIT_SECONDS - (now - valid_history[0])) + 1
            return False, max(1, wait_sec)
        REQUEST_HISTORY[client_ip].append(now)
        return True, 0

    def get_client_ip(self):
        # Support Cloudflare True-Client-IP and X-Forwarded-For
        cf_ip = self.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()
        x_forwarded = self.headers.get("X-Forwarded-For")
        if x_forwarded:
            return x_forwarded.split(",")[0].strip()
        return self.client_address[0]

    def send_json(self, status_code, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Access-Password")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Access-Password")
        self.end_headers()

    def do_HEAD(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        if path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
        elif path.startswith("/api/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        # Health & Stats
        if path == "/api/health":
            stats = get_cache_stats()
            self.send_json(200, {
                "status": "healthy",
                "service": "Card BIN Multi-Engine Split Gateway",
                "engines": {
                    "free_8digit": "HandyAPI + Binlist (No Quota Cost)",
                    "precision_10digit": "Neutrino API (User: wexnsk)",
                    "cache": "SQLite Persistent Storage"
                },
                "auth_enabled": AUTH_ENABLED,
                "rate_limit_enabled": RATE_LIMIT_ENABLED,
                "timestamp": int(time.time()),
                "cache": stats
            })
            return

        # Password Verification Endpoint
        if path == "/api/verify":
            if self.check_auth():
                self.send_json(200, {"success": True, "message": "访问密码验证通过 (Authorized)"})
            else:
                self.send_json(401, {"success": False, "error": "访问密码错误，请重新输入"})
            return

        # Lookup API via GET (Password protected)
        if path == "/api/lookup":
            if not self.check_auth():
                self.send_json(401, {"success": False, "error": "访问受限：需要授权密码 (Access password required)"})
                return
            bin_param = query.get("bin", [""])[0]
            self.handle_bin_lookup(bin_param)
            return

        # Frontend Homepage
        if path in ["/", "/index.html"]:
            self.serve_frontend()
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8", errors="ignore")
        try:
            data = json.loads(post_body) if post_body else {}
        except Exception:
            data = {}

        # Password Verification
        if path == "/api/verify":
            if self.check_auth(data):
                self.send_json(200, {"success": True, "message": "访问密码验证通过 (Authorized)"})
            else:
                self.send_json(401, {"success": False, "error": "访问密码错误，请重新输入"})
            return

        # Protected Endpoints Check Auth
        if not self.check_auth(data):
            self.send_json(401, {"success": False, "error": "访问受限：需要授权密码 (Access password required)"})
            return

        if path == "/api/lookup":
            bin_param = data.get("bin", "")
            self.handle_bin_lookup(bin_param)
            return

        if path == "/api/batch":
            bins = data.get("bins", [])
            if not isinstance(bins, list) or len(bins) == 0:
                self.send_json(400, {"success": False, "error": "请提供有效 BIN 数组 (bins parameter must be an array)"})
                return
            
            # Limit batch size to 20
            bins = bins[:20]
            results = []
            for b in bins:
                clean = "".join(filter(str.isdigit, str(b)))[:10]
                if len(clean) < 6:
                    continue
                # 1. Cache priority
                cached_res, is_cached = get_from_cache(clean)
                if cached_res:
                    results.append(enrich_result(clean, cached_res, is_cached=True))
                else:
                    # 2. Dispatcher
                    routed_data = dispatch_bin_lookup(clean)
                    save_to_cache(clean, routed_data)
                    results.append(enrich_result(clean, routed_data, is_cached=False))
            
            self.send_json(200, {"success": True, "count": len(results), "items": results})
            return

        self.send_response(404)
        self.end_headers()

    def handle_bin_lookup(self, bin_param):
        clean_bin = "".join(filter(str.isdigit, str(bin_param)))
        if len(clean_bin) < 6:
            self.send_json(400, {
                "success": False,
                "error": "卡号/BIN 位数过短，请输入至少 6 到 10 位有效数字 (Minimum 6 digits required)"
            })
            return

        query_bin = clean_bin[:10]

        # 1. 本地 SQLite 缓存检查 (0 外部耗时，0 外部额度)
        cached_data, is_cached = get_from_cache(query_bin)
        if cached_data:
            enriched = enrich_result(query_bin, cached_data, is_cached=True)
            self.send_json(200, enriched)
            return

        # 2. 智能分流引擎处理
        routed_data = dispatch_bin_lookup(query_bin)
        save_to_cache(query_bin, routed_data)
        enriched = enrich_result(query_bin, routed_data, is_cached=False)
        self.send_json(200, enriched)

    def serve_frontend(self):
        index_file = os.path.join(DATA_DIR, "index.html")
        if os.path.exists(index_file):
            with open(index_file, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = "<h1>Card BIN Lookup Service Ready</h1>"

        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def run():
    init_db()
    server = ThreadedHTTPServer((HOST, PORT), BinRequestHandler)
    print(f"🚀 Card BIN Multi-Engine Gateway running on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    run()

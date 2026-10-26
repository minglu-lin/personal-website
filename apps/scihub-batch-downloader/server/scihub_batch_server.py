#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献批量获取与打包平台 (v6.0 纯开放文献库高并发版 · 针对 2 OCPU / 12GB RAM / 2Gbps VPS 深度调优)
历史版本归档说明:
  - v5.0 (开源全量库 + 北大 CARSI/WebVPN + Camoufox 远程过盾版) 已完整归档至:
    本地: 05_Scripts/scihub_batch_server_v5_pku_carsi_archive.py
    云端: /opt/scihub_batch/server_v5_pku_carsi_archive.py
  - v4.0 (双 Telegram Bot 补抓版) 已完整归档至:
    本地: 05_Scripts/scihub_batch_server_v4_tgbot_archive.py
    云端: /opt/scihub_batch/server_v4_tgbot_archive.py

本版 (v6.0) 核心特性:
  1. 网页端纯粹极简：只需上传或粘贴 DOI，一键通过全量开放文献库批量下载并打包 ZIP，无需任何账号登录或过盾交互。
  2. 全量开放文献库矩阵：
     - Sci-Hub 四镜像负载均衡池 (sci-hub.ru / .se / .st / .wf) + 独立会话 Cookie + 纯 Python Altcha SHA-256 算力验证码自动秒解
     - OpenAlex 全位置 OA PDF 挖掘 (Best OA + 机构仓库 + 预印本)
     - Unpaywall 全量开放知识库挖掘 (高校机构库、arXiv/bioRxiv/medRxiv/ChemRxiv/PMC 录用稿与预印本)
     - Crossref 全文直链 + EuropePMC / PubMed Central + Semantic Scholar OA
     - 主流出版商与预印本规律直链合成 (Nature / Springer / PLOS / BMC / Frontiers / arXiv / bioRxiv / medRxiv)
     - Handle.net 官方无盾 API 解析真实落地页 + curl_cffi Chrome 124 TLS 指纹嗅探 citation_pdf_url
  3. 针对圣何塞甲骨文 ARM (2 OCPU / 12GB RAM / 2Gbps 带宽) 深度高并发优化：
     - 突破 Python 默认 ThreadPoolExecutor 在 2 核机器上仅 6 线程 (cpu_count+4) 的隐式瓶颈，显式挂载 64 线程高速 I/O 池
     - 默认并发 Worker 从 3 路提升至 16 路（支持前端 8 / 12 / 16 / 24 路自由切换）
     - 单篇 DOI 元数据双源并行检索（OpenAlex 与 Unpaywall 并发拉取，耗时减半）
     - Sci-Hub 多镜像轮询分流（16 路并发自动打散至 4 个镜像，避免单镜像连接拥塞）
     - 按需增量 ZIP 归档（消除每下一篇文献就全量重压缩一遍 ZIP 的磁盘与 CPU 锁竞争）
"""

import asyncio
import base64
import csv
import hashlib
import html as html_lib
import http.cookiejar
import io
import json
import os
import random
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except Exception:
    import requests as cffi_requests
    HAS_CFFI = False

BASE_DIR = "/opt/scihub_batch"
DATA_DIR = os.path.join(BASE_DIR, "data")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
TASKS_INDEX_FILE = os.path.join(DATA_DIR, "tasks_index.json")

os.makedirs(TASKS_DIR, exist_ok=True)

# 针对 2 OCPU + 12GB RAM + 2Gbps 带宽的高并发参数配置
DEFAULT_CONCURRENCY = 16
MAX_CONCURRENCY = 28
MIN_CONCURRENCY = 4
IO_THREAD_POOL_SIZE = 64

GLOBAL_IO_EXECUTOR = ThreadPoolExecutor(max_workers=IO_THREAD_POOL_SIZE, thread_name_prefix="sci_oa_io")

app = FastAPI(title="Literature DOI Batch Downloader (High-Concurrency Open-Source Edition)", version="6.0.0")

# ==================== 全局任务与多用户排队状态 ====================
TASKS: Dict[str, dict] = {}
PENDING_TASK_IDS: List[str] = []      # 排队中的 task_id 列表 (FIFO)
ACTIVE_TASK_ID: Optional[str] = None  # 当前正在执行的 task_id
QUEUE_EVENT = asyncio.Event()         # 唤醒主队列调度器
ZIP_BUILD_LOCKS: Dict[str, asyncio.Lock] = {}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# ==================== Sci-Hub 多镜像池（独立会话与算力锁，支持 16~24 路并发分流） ====================
SCIHUB_MIRRORS = [
    "https://sci-hub.ru",
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.wf",
]

MIRROR_SESSIONS: Dict[str, dict] = {}
for _m in SCIHUB_MIRRORS:
    _jar = http.cookiejar.CookieJar()
    _opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
    _opener.addheaders = [("User-Agent", USER_AGENT)]
    MIRROR_SESSIONS[_m] = {
        "jar": _jar,
        "opener": _opener,
        "pow_lock": threading.Lock(),
    }


def save_tasks_index():
    try:
        with open(TASKS_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(TASKS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] 保存任务索引失败: {e}")


def load_tasks_index():
    global TASKS
    if os.path.exists(TASKS_INDEX_FILE):
        try:
            with open(TASKS_INDEX_FILE, "r", encoding="utf-8") as f:
                TASKS = json.load(f)
            for tid, t in TASKS.items():
                if t.get("status") in ("running", "queued"):
                    t["status"] = "interrupted"
                    t["step_msg"] = "服务重启，已抓取部分可直接下载压缩包"
                    t["zip_dirty"] = True
        except Exception as e:
            print(f"[!] 读取任务索引失败: {e}")


DOI_REGEX = re.compile(r'(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', re.IGNORECASE)


def extract_dois_from_text(raw_text: str) -> List[str]:
    seen = set()
    result = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = urllib.parse.unquote(line)
        matches = DOI_REGEX.findall(line)
        for m in matches:
            clean_doi = m.rstrip(".,;)]}>'\"")
            key = clean_doi.lower()
            if key not in seen:
                seen.add(key)
                result.append(clean_doi)
    return result


def sanitize_filename(name: str, max_len: int = 120) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(" ._")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].strip(" ._")
    return cleaned or "paper"


def is_valid_pdf(data: bytes) -> bool:
    if not data or len(data) < 900:
        return False
    head = data[:1024]
    return b"%PDF-" in head


# ==================== 开放文献库抓取核心函数 ====================

def http_get_bytes(url: str, timeout: int = 18, headers: Optional[dict] = None) -> Optional[bytes]:
    """线程安全的通用 HTTP 字节获取（内置 curl_cffi Chrome 124 TLS 指纹回退与 Akamai PoW 秒解）"""
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if data and is_valid_pdf(data):
                return data
            if data and not HAS_CFFI:
                return data
    except Exception:
        pass

    if HAS_CFFI:
        try:
            with cffi_requests.Session(impersonate="chrome124", verify=False) as s_cffi:
                r = s_cffi.get(url, headers=req_headers, timeout=timeout, allow_redirects=True)
                if r.status_code == 200 and r.content:
                    if b"/_sec/verify?provider=interstitial" in r.content[:15000]:
                        txt = r.text
                        m_bm = re.search(r'"bm-verify"\s*:\s*"([^"]+)"', txt)
                        m_i = re.search(r'var\s+i\s*=\s*(\d+)\s*;', txt)
                        m_nums = re.search(r'Number\(\s*"(\d+)"\s*\+\s*"(\d+)"\s*\)', txt)
                        if m_bm and m_i and m_nums:
                            pow_val = int(m_i.group(1)) + int(m_nums.group(1) + m_nums.group(2))
                            parsed_u = urllib.parse.urlparse(r.url)
                            verify_u = f"{parsed_u.scheme}://{parsed_u.netloc}/_sec/verify?provider=interstitial"
                            s_cffi.post(
                                verify_u,
                                json={"bm-verify": m_bm.group(1), "pow": pow_val},
                                headers={"Referer": r.url, "Origin": f"{parsed_u.scheme}://{parsed_u.netloc}"},
                                timeout=12
                            )
                            r2 = s_cffi.get(r.url, headers=req_headers, timeout=timeout, allow_redirects=True)
                            if r2.status_code == 200 and r2.content:
                                return r2.content
                    return r.content
        except Exception:
            pass
    return None


def http_get_json(url: str, timeout: int = 8) -> Optional[dict]:
    req_headers = {
        "User-Agent": "SciBatchOA/6.0 (mailto:research@example.com)",
        "Accept": "application/json",
    }
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return None


def download_url_if_pdf_sync(url: str, opener=None, timeout: int = 22) -> Optional[bytes]:
    try:
        if opener is None:
            raw = http_get_bytes(url, timeout=timeout)
        else:
            with opener.open(url, timeout=timeout) as resp:
                raw = resp.read()
        if raw and len(raw) > 1024 and b"%PDF-" in raw[:1024]:
            idx = raw.find(b"%PDF-")
            return raw[idx:]
    except Exception:
        pass
    return None


def solve_scihub_altcha_if_needed(base_mirror: str, html: str) -> bool:
    """自动识别并求解 Sci-Hub 的 Altcha SHA-256 工作量证明验证码（按镜像独立加锁）"""
    c_match = re.search(r"challengeurl\s*=\s*[\"\'](/captcha/challenge/\d+)[\"\']", html)
    s_match = re.search(r"fetch\([\"\'](/captcha/solution/\d+)[\"\']", html)
    if not (c_match and s_match):
        return False

    ms = MIRROR_SESSIONS.get(base_mirror)
    if not ms:
        return False

    opener = ms["opener"]
    with ms["pow_lock"]:
        try:
            ch_url = base_mirror.rstrip("/") + c_match.group(1)
            ch_raw = opener.open(ch_url, timeout=10).read().decode("utf-8", "ignore")
            ch_data = json.loads(ch_raw)
            salt = ch_data["salt"]
            target = ch_data["challenge"]
            max_n = int(ch_data.get("maxNumber", ch_data.get("maxnumber", 500000)))

            for n in range(max_n + 1):
                if hashlib.sha256(f"{salt}{n}".encode()).hexdigest() == target:
                    payload = base64.b64encode(json.dumps({
                        "algorithm": ch_data.get("algorithm", "SHA-256"),
                        "challenge": target,
                        "number": n,
                        "salt": salt,
                        "signature": ch_data["signature"],
                        "took": random.randint(25, 85)
                    }).encode()).decode()

                    sol_url = base_mirror.rstrip("/") + s_match.group(1)
                    req = urllib.request.Request(
                        sol_url,
                        data=json.dumps({"captcha": payload}).encode("utf-8"),
                        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
                    )
                    res_raw = opener.open(req, timeout=10).read().decode("utf-8", "ignore")
                    res_json = json.loads(res_raw)
                    return bool(res_json.get("success"))
        except Exception as e:
            print(f"[!] Altcha PoW 求解异常 ({base_mirror}): {e}")
    return False


def fetch_from_scihub_mirrors_sync(doi: str, worker_hint: int = 0) -> Tuple[Optional[bytes], str]:
    """
    从 Sci-Hub 镜像池下载 PDF：
      - 根据 worker_hint 自动轮询打散起始镜像，16 路并发均匀分布在 4 个镜像上
      - 若遇到 Altcha SHA-256 算力验证则自动秒解并固化 Cookie
    """
    n_mirrors = len(SCIHUB_MIRRORS)
    start_idx = worker_hint % n_mirrors
    ordered_mirrors = SCIHUB_MIRRORS[start_idx:] + SCIHUB_MIRRORS[:start_idx]

    for mirror in ordered_mirrors:
        ms = MIRROR_SESSIONS[mirror]
        opener = ms["opener"]
        try:
            target_url = f"{mirror.rstrip('/')}/{doi}"
            html = opener.open(target_url, timeout=14).read().decode("utf-8", "ignore")

            if "altcha-widget" in html or "/captcha/challenge/" in html:
                if solve_scihub_altcha_if_needed(mirror, html):
                    html = opener.open(target_url, timeout=14).read().decode("utf-8", "ignore")

            pdf_candidates = []
            for pattern in [
                r'(?:src|href|data)\s*=\s*["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
                r"location\.href\s*=\s*['\"]([^'\"]+\.pdf(?:\?[^'\"]*)?)['\"]",
                r'(/storage/[^"\'>\s]+\.pdf)',
                r'(/downloads/[^"\'>\s]+\.pdf)',
            ]:
                for m in re.finditer(pattern, html, re.IGNORECASE):
                    cand = m.group(1).split("#")[0]
                    if cand not in pdf_candidates:
                        pdf_candidates.append(cand)

            for raw_pdf_url in pdf_candidates:
                if raw_pdf_url.startswith("//"):
                    pdf_url = "https:" + raw_pdf_url
                elif raw_pdf_url.startswith("/"):
                    pdf_url = mirror.rstrip("/") + raw_pdf_url
                elif not raw_pdf_url.startswith("http"):
                    pdf_url = urllib.parse.urljoin(target_url, raw_pdf_url)
                else:
                    pdf_url = raw_pdf_url

                pdf_bytes = download_url_if_pdf_sync(pdf_url, opener=opener, timeout=25)
                if pdf_bytes:
                    mirror_host = urllib.parse.urlparse(mirror).netloc
                    return pdf_bytes, f"Sci-Hub ({mirror_host})"
        except Exception:
            continue
    return None, ""


def fetch_metadata_and_oa_sync(doi: str) -> dict:
    """
    双源并行检索论文标题、年份、作者以及全量 OA 候选链接：
      - OpenAlex API 与 Unpaywall API 同时并发请求（耗时减半）
      - 若缺少标题，自动通过 Crossref API 补齐元数据与全文直链
    """
    info = {
        "doi": doi,
        "title": "",
        "year": "",
        "author": "",
        "oa_pdf_urls": []  # List of (source_label, pdf_url)
    }
    q_doi = urllib.parse.quote(doi, safe="")
    seen_urls = set()

    def add_candidate(label: str, u: Optional[str]):
        if u and isinstance(u, str) and u.startswith("http") and u not in seen_urls:
            seen_urls.add(u)
            info["oa_pdf_urls"].append((label, u))

    oa_url = f"https://api.openalex.org/works/https://doi.org/{q_doi}"
    unpaywall_url = f"https://api.unpaywall.org/v2/{q_doi}?email=scholar_oa@example.com"

    # 并行请求 OpenAlex 与 Unpaywall
    fut_oa = GLOBAL_IO_EXECUTOR.submit(http_get_json, oa_url, 7)
    fut_upw = GLOBAL_IO_EXECUTOR.submit(http_get_json, unpaywall_url, 7)

    oa_data = None
    upw = None
    try:
        oa_data = fut_oa.result(timeout=8)
    except Exception:
        pass
    try:
        upw = fut_upw.result(timeout=8)
    except Exception:
        pass

    # 1. 解析 OpenAlex 结果
    if oa_data and isinstance(oa_data, dict):
        if not info["title"]:
            info["title"] = (oa_data.get("title") or "").strip()
        if not info["year"] and oa_data.get("publication_year"):
            info["year"] = str(oa_data.get("publication_year"))
        authorships = oa_data.get("authorships") or []
        if not info["author"] and authorships and isinstance(authorships[0], dict):
            author_obj = authorships[0].get("author") or {}
            info["author"] = author_obj.get("display_name") or ""

        oa_info = oa_data.get("open_access") or {}
        add_candidate("OpenAlex OA", oa_info.get("oa_url"))
        best_oa = oa_data.get("best_oa_location") or {}
        if isinstance(best_oa, dict):
            add_candidate("OpenAlex (BestPDF)", best_oa.get("pdf_url"))
        for loc in (oa_data.get("locations") or []):
            if isinstance(loc, dict):
                add_candidate("OpenAlex (机构库)", loc.get("pdf_url"))

    # 2. 解析 Unpaywall 结果
    if upw and isinstance(upw, dict):
        if not info["title"] and upw.get("title"):
            info["title"] = str(upw["title"]).strip()
        if not info["year"] and upw.get("year"):
            info["year"] = str(upw["year"])
        z_authors = upw.get("z_authors") or []
        if not info["author"] and z_authors and isinstance(z_authors, list):
            first_a = z_authors[0]
            if isinstance(first_a, dict):
                info["author"] = first_a.get("family") or first_a.get("given") or ""
        best_loc = upw.get("best_oa_location") or {}
        if isinstance(best_loc, dict):
            add_candidate("Unpaywall (最佳OA)", best_loc.get("url_for_pdf"))
        for loc in (upw.get("oa_locations") or []):
            if isinstance(loc, dict):
                host_type = loc.get("host_type") or "OA仓库"
                add_candidate(f"Unpaywall ({host_type})", loc.get("url_for_pdf"))

    # 3. Crossref 补齐元数据与 PDF link
    if not info["title"]:
        cr_url = f"https://api.crossref.org/works/{q_doi}"
        cr_data = http_get_json(cr_url, timeout=7)
        if cr_data and isinstance(cr_data.get("message"), dict):
            msg = cr_data["message"]
            titles = msg.get("title") or []
            if titles and not info["title"]:
                info["title"] = titles[0].strip()
            issued = (msg.get("issued") or {}).get("date-parts") or []
            if issued and issued[0] and not info["year"]:
                info["year"] = str(issued[0][0])
            authors = msg.get("author") or []
            if authors and not info["author"]:
                a0 = authors[0]
                info["author"] = f"{a0.get('family', '')} {a0.get('given', '')}".strip()
            for lk in (msg.get("link") or []):
                if isinstance(lk, dict) and "pdf" in (lk.get("content-type") or "").lower():
                    u = lk.get("URL")
                    if u and "api.elsevier.com" not in u:
                        add_candidate("Crossref 直链", u)

    return info


def resolve_doi_target_url_sync(doi: str) -> str:
    """通过 Handle.net 官方 JSON API 解析 DOI 对应的出版商真实落地页 URL"""
    api_url = f"https://doi.org/api/handles/{urllib.parse.quote(doi)}"
    data = http_get_json(api_url, timeout=8)
    if data and isinstance(data, dict):
        for val in data.get("values", []):
            if isinstance(val, dict) and val.get("type") == "URL":
                u = (val.get("data") or {}).get("value")
                if u and isinstance(u, str) and u.startswith("http"):
                    return u
    return ""


def try_direct_publisher_patterns_sync(doi: str) -> Tuple[Optional[bytes], str]:
    """
    针对 EuropePMC + SemanticScholar (并发查询) + 常见出版商/预印本规律直链合成 + 落地页 citation_pdf_url 提取
    """
    q_doi = urllib.parse.quote(doi, safe="")

    epmc_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{q_doi}&format=json&resultType=core"
    s2_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{q_doi}?fields=openAccessPdf"

    fut_epmc = GLOBAL_IO_EXECUTOR.submit(http_get_json, epmc_url, 7)
    fut_s2 = GLOBAL_IO_EXECUTOR.submit(http_get_json, s2_url, 6)

    # 1. EuropePMC
    try:
        epmc_data = fut_epmc.result(timeout=8)
        if epmc_data and isinstance(epmc_data, dict):
            res_list = (epmc_data.get("resultList") or {}).get("result") or []
            if res_list and isinstance(res_list[0], dict):
                pmcid = res_list[0].get("pmcid")
                if pmcid:
                    pdf_b = download_url_if_pdf_sync(
                        f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf",
                        timeout=18
                    )
                    if pdf_b:
                        return pdf_b, f"EuropePMC ({pmcid})"
    except Exception:
        pass

    # 2. Semantic Scholar
    try:
        s2_data = fut_s2.result(timeout=7)
        if s2_data and isinstance(s2_data, dict):
            oa_pdf = (s2_data.get("openAccessPdf") or {}).get("url")
            if oa_pdf:
                pdf_b = download_url_if_pdf_sync(oa_pdf, timeout=18)
                if pdf_b:
                    return pdf_b, "SemanticScholar OA"
    except Exception:
        pass

    # 3. 常见出版社与预印本库规律直链
    doi_low = doi.lower()
    pattern_candidates = []
    if doi_low.startswith("10.48550/arxiv."):
        arxiv_id = doi_low.split("10.48550/arxiv.", 1)[1]
        pattern_candidates.append(("arXiv 预印本直链", f"https://arxiv.org/pdf/{arxiv_id}.pdf"))
    elif doi_low.startswith("10.1101/"):
        pattern_candidates.append(("bioRxiv 预印本直链", f"https://www.biorxiv.org/content/{doi}.full.pdf"))
        pattern_candidates.append(("medRxiv 预印本直链", f"https://www.medrxiv.org/content/{doi}.full.pdf"))
    elif doi_low.startswith("10.1038/"):
        suffix = doi.split("/", 1)[1]
        pattern_candidates.append(("Nature OA 直链", f"https://www.nature.com/articles/{suffix}.pdf"))
    elif doi_low.startswith("10.1007/"):
        pattern_candidates.append(("Springer OA 直链", f"https://link.springer.com/content/pdf/{urllib.parse.quote(doi)}.pdf"))
    elif doi_low.startswith("10.1371/"):
        pattern_candidates.append(("PLOS OA 直链", f"https://journals.plos.org/plosone/article/file?id={urllib.parse.quote(doi)}&type=printable"))
    elif doi_low.startswith("10.1186/"):
        pattern_candidates.append(("BMC OA 直链", f"https://bmcpublichealth.biomedcentral.com/counter/pdf/{urllib.parse.quote(doi)}.pdf"))
    elif doi_low.startswith("10.3389/"):
        pattern_candidates.append(("Frontiers OA 直链", f"https://www.frontiersin.org/articles/{doi}/pdf"))

    for label, p_url in pattern_candidates:
        raw = http_get_bytes(p_url, timeout=16)
        if raw and is_valid_pdf(raw):
            return raw, label

    # 4. 通过 Handle.net 解析真实落地页 URL，再嗅探 citation_pdf_url
    try:
        target_u = resolve_doi_target_url_sync(doi) or f"https://doi.org/{urllib.parse.quote(doi)}"
        if HAS_CFFI:
            with cffi_requests.Session(impersonate="chrome124", verify=False) as s_cffi:
                r = s_cffi.get(target_u, timeout=12, allow_redirects=True)
                if r.status_code == 200 and r.content:
                    if is_valid_pdf(r.content):
                        return r.content, "DOI直接返回PDF"
                    html_str = r.text[:200000]
                    m = re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']', html_str, re.IGNORECASE)
                    if not m:
                        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']', html_str, re.IGNORECASE)
                    if m:
                        pdf_url = urllib.parse.urljoin(r.url, html_lib.unescape(m.group(1)))
                        raw2 = http_get_bytes(pdf_url, timeout=16, headers={"Referer": r.url})
                        if raw2 and is_valid_pdf(raw2):
                            return raw2, "出版商落地页 OA"
    except Exception:
        pass

    return None, ""


async def fetch_via_opensource_libraries(doi: str, meta: dict, worker_hint: int = 0) -> Tuple[Optional[bytes], str]:
    """
    全量开放文献库高速流水线：
      1. 优先第一顺位：Sci-Hub 四镜像负载均衡池 (含 Altcha SHA-256 算力自动秒解)
      2. 第二顺位：Unpaywall + OpenAlex + Crossref 全量机构知识库/预印本 OA 直链
      3. 第三顺位：EuropePMC + SemanticScholar + 主流出版社规律直链 + DOI 落地页 citation_pdf_url
    """
    # 1. 第一顺位尝试 Sci-Hub 镜像池（按 worker_hint 轮询打散镜像）
    sh_bytes, sh_src = await asyncio.to_thread(fetch_from_scihub_mirrors_sync, doi, worker_hint)
    if sh_bytes and is_valid_pdf(sh_bytes):
        return sh_bytes, sh_src

    # 2. 遍历所有 Unpaywall / OpenAlex / Crossref 候选链接
    for source_label, oa_url in meta.get("oa_pdf_urls", []):
        raw = await asyncio.to_thread(http_get_bytes, oa_url, 18)
        if raw and is_valid_pdf(raw):
            return raw, source_label
        if raw and len(raw) < 300000:
            txt = raw.decode("utf-8", errors="ignore")
            m = re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']', txt, re.IGNORECASE)
            if m:
                real_pdf_url = urllib.parse.urljoin(oa_url, m.group(1))
                raw2 = await asyncio.to_thread(http_get_bytes, real_pdf_url, 18)
                if raw2 and is_valid_pdf(raw2):
                    return raw2, source_label

    # 3. 尝试 EuropePMC / SemanticScholar / 主流出版商规律直链与落地页嗅探
    pub_bytes, pub_src = await asyncio.to_thread(try_direct_publisher_patterns_sync, doi)
    if pub_bytes and is_valid_pdf(pub_bytes):
        return pub_bytes, pub_src

    return None, ""


# ==================== ZIP 归档打包与高并发任务调度引擎 ====================

def get_task_zip_lock(task_id: str) -> asyncio.Lock:
    if task_id not in ZIP_BUILD_LOCKS:
        ZIP_BUILD_LOCKS[task_id] = asyncio.Lock()
    return ZIP_BUILD_LOCKS[task_id]


def build_task_zip(task_id: str) -> str:
    task = TASKS.get(task_id)
    if not task:
        return ""
    task_dir = os.path.join(TASKS_DIR, task_id)
    pdf_dir = os.path.join(task_dir, "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)
    zip_path = os.path.join(task_dir, f"DOI_Batch_{task_id}.zip")

    report_csv_buf = io.StringIO()
    report_csv_buf.write("\ufeff")
    writer = csv.writer(report_csv_buf)
    writer.writerow(["序号", "DOI", "抓取结果", "命中开放库渠道", "年份", "第一作者", "论文标题", "保存文件名", "文件大小(KB)"])

    failed_dois = []
    for idx, item in enumerate(task.get("items", []), start=1):
        st_label = "成功" if item["status"] == "success" else ("未命中" if item["status"] == "failed" else "待处理")
        writer.writerow([
            idx,
            item["doi"],
            st_label,
            item.get("source") or "-",
            item.get("year") or "-",
            item.get("author") or "-",
            item.get("title") or "-",
            item.get("filename") or "-",
            item.get("size_kb") or 0
        ])
        if item["status"] == "failed":
            failed_dois.append(item["doi"])

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(os.listdir(pdf_dir)):
            fpath = os.path.join(pdf_dir, fname)
            if os.path.isfile(fpath) and fname.lower().endswith(".pdf"):
                zf.write(fpath, arcname=f"PDFs/{fname}")
        zf.writestr("00_文献抓取汇总清单.csv", report_csv_buf.getvalue().encode("utf-8"))
        if failed_dois:
            zf.writestr("01_未命中DOI列表.txt", "\n".join(failed_dois).encode("utf-8"))

    task["zip_path"] = zip_path
    task["zip_ready"] = True
    task["zip_dirty"] = False
    return zip_path


def save_pdf_to_task(task: dict, item: dict, pdf_bytes: bytes, source_used: str, pdf_dir: str):
    doi = item["doi"]
    naming_mode = task.get("naming_mode", "doi_title")
    if naming_mode == "doi_only" or not item.get("title"):
        base_name = sanitize_filename(doi.replace("/", "_"), max_len=100)
    else:
        prefix = f"{item['year']}_" if item.get("year") else ""
        author_part = f"{sanitize_filename(item['author'], 25)}_" if item.get("author") else ""
        title_part = sanitize_filename(item["title"], 75)
        doi_short = sanitize_filename(doi.replace("/", "_"), 35)
        base_name = f"{prefix}{author_part}{title_part}_({doi_short})"

    filename = f"{base_name}.pdf"
    fpath = os.path.join(pdf_dir, filename)
    if os.path.exists(fpath):
        filename = f"{base_name}_{uuid.uuid4().hex[:4]}.pdf"
        fpath = os.path.join(pdf_dir, filename)

    with open(fpath, "wb") as f:
        f.write(pdf_bytes)

    item["status"] = "success"
    item["source"] = source_used
    item["filename"] = filename
    item["size_kb"] = round(len(pdf_bytes) / 1024, 1)
    task["success_count"] = task.get("success_count", 0) + 1
    task["zip_ready"] = True
    task["zip_dirty"] = True


async def execute_single_task(task_id: str):
    """
    高并发开放文献库抓取引擎：
      - 根据任务设定的并发路数 (默认 16 路，最高 28 路) 同时启动异步 Worker
      - 配合底层 64 线程 I/O 池与双源并行元数据拉取，最大化圣何塞 2C/12G/2Gbps 实例吞吐量
    """
    task = TASKS.get(task_id)
    if not task:
        return

    task["status"] = "running"
    task["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_dir = os.path.join(TASKS_DIR, task_id)
    pdf_dir = os.path.join(task_dir, "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)

    items = task.get("items", [])
    total = len(items)
    concurrency = max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, int(task.get("concurrency") or DEFAULT_CONCURRENCY)))
    actual_workers = min(concurrency, max(1, total))
    task["concurrency"] = concurrency

    file_lock = asyncio.Lock()
    task["phase"] = "running_oa"
    task["step_msg"] = f"正在以 {actual_workers} 路并发高速检索开放文献库 (Sci-Hub + Unpaywall + OpenAlex + PMC)..."
    save_tasks_index()

    work_queue: asyncio.Queue = asyncio.Queue()
    for idx in range(total):
        work_queue.put_nowait(idx)

    processed_cnt = 0
    last_index_save_ts = 0.0

    async def oa_worker(w_id: int):
        nonlocal processed_cnt, last_index_save_ts
        # 轻微错峰启动，避免所有协程在同一毫秒爆发连接
        if w_id > 1:
            await asyncio.sleep(min(0.8, (w_id - 1) * 0.04))

        while not work_queue.empty():
            if task.get("cancel_requested"):
                break
            try:
                idx = work_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            item = items[idx]
            doi = item["doi"]
            item["status"] = "fetching"
            task["current_doi"] = doi

            meta = await asyncio.to_thread(fetch_metadata_and_oa_sync, doi)
            item["title"] = meta.get("title") or ""
            item["year"] = meta.get("year") or ""
            item["author"] = meta.get("author") or ""

            pdf_bytes, source_used = await fetch_via_opensource_libraries(doi, meta, worker_hint=idx + w_id)

            async with file_lock:
                processed_cnt += 1
                task["done_count"] = processed_cnt
                if pdf_bytes:
                    await asyncio.to_thread(save_pdf_to_task, task, item, pdf_bytes, source_used, pdf_dir)
                else:
                    item["status"] = "failed"
                    item["source"] = "开放文献库未命中"
                    task["failed_count"] = task.get("failed_count", 0) + 1

                task["step_msg"] = (
                    f"【{actual_workers} 路并发加速中 ({processed_cnt}/{total})】"
                    f"已命中 {task.get('success_count', 0)} 篇 · 最新完成: {doi}"
                )
                now_ts = time.time()
                if now_ts - last_index_save_ts >= 1.2 or processed_cnt == total:
                    last_index_save_ts = now_ts
                    save_tasks_index()

            work_queue.task_done()

    await asyncio.gather(*(oa_worker(i) for i in range(1, actual_workers + 1)))

    task["phase"] = "done"
    if task.get("cleared"):
        TASKS.pop(task_id, None)
        shutil.rmtree(task_dir, ignore_errors=True)
        save_tasks_index()
        return

    if task.get("cancel_requested"):
        task["status"] = "cancelled"
        task["step_msg"] = f"已手动停止并完成打包（成功抓取 {task.get('success_count', 0)}/{total} 篇）"
    else:
        task["status"] = "completed"
        task["step_msg"] = f"全部处理完成！共成功抓取 {task.get('success_count', 0)}/{total} 篇，未命中 {task.get('failed_count', 0)} 篇。"
    task["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with get_task_zip_lock(task_id):
        await asyncio.to_thread(build_task_zip, task_id)

    if task.get("cleared"):
        TASKS.pop(task_id, None)
        shutil.rmtree(task_dir, ignore_errors=True)
    save_tasks_index()


async def master_queue_scheduler():
    global ACTIVE_TASK_ID
    while True:
        if not PENDING_TASK_IDS:
            QUEUE_EVENT.clear()
            await QUEUE_EVENT.wait()

        while PENDING_TASK_IDS:
            next_tid = PENDING_TASK_IDS.pop(0)
            task = TASKS.get(next_tid)
            if not task or task.get("status") == "cancelled":
                continue
            ACTIVE_TASK_ID = next_tid
            try:
                await execute_single_task(next_tid)
            except Exception as e:
                print(f"[!] 执行任务 {next_tid} 出错: {e}")
                task["status"] = "interrupted"
                task["step_msg"] = f"执行异常中断: {e}"
                save_tasks_index()
            finally:
                ACTIVE_TASK_ID = None


def compute_queue_info_for_task(target_tid: str) -> dict:
    task = TASKS.get(target_tid)
    if not task or task.get("status") != "queued":
        return {"queue_position": 0, "ahead_tasks": 0, "ahead_dois": 0, "est_wait_seconds": 0}

    ahead_tasks = 0
    ahead_dois = 0
    est_wait_seconds = 0

    if ACTIVE_TASK_ID and ACTIVE_TASK_ID != target_tid:
        act = TASKS.get(ACTIVE_TASK_ID)
        if act and act.get("status") == "running":
            ahead_tasks += 1
            rem = max(0, act.get("total", 0) - (act.get("success_count", 0) + act.get("failed_count", 0)))
            ahead_dois += rem
            est_wait_seconds += max(2, int(rem * 0.6))

    for q_tid in PENDING_TASK_IDS:
        if q_tid == target_tid:
            break
        qt = TASKS.get(q_tid)
        if qt and qt.get("status") == "queued":
            ahead_tasks += 1
            rem = qt.get("total", 0)
            ahead_dois += rem
            est_wait_seconds += max(2, int(rem * 0.6))

    return {
        "queue_position": ahead_tasks,
        "ahead_tasks": ahead_tasks,
        "ahead_dois": ahead_dois,
        "est_wait_seconds": est_wait_seconds
    }


def get_public_overview() -> dict:
    busy = ACTIVE_TASK_ID is not None
    active_summary = None
    if ACTIVE_TASK_ID and ACTIVE_TASK_ID in TASKS:
        act = TASKS[ACTIVE_TASK_ID]
        done = act.get("success_count", 0) + act.get("failed_count", 0)
        rem = max(0, act.get("total", 0) - done)
        active_summary = {
            "task_id": act["task_id"],
            "title": act.get("title", "文献批次"),
            "total": act.get("total", 0),
            "done": done,
            "remaining": rem,
            "concurrency": act.get("concurrency", DEFAULT_CONCURRENCY),
            "current_doi": act.get("current_doi", ""),
            "step_msg": act.get("step_msg", ""),
            "started_at": act.get("started_at", "")
        }
    queued_list = []
    for pos, q_tid in enumerate(PENDING_TASK_IDS, start=1):
        qt = TASKS.get(q_tid)
        if qt:
            queued_list.append({
                "position": pos,
                "task_id": q_tid,
                "title": qt.get("title", ""),
                "total": qt.get("total", 0),
                "created_at": qt.get("created_at", "")
            })

    return {
        "system_busy": busy,
        "active_task": active_summary,
        "queued_count": len(queued_list),
        "queued_tasks": queued_list,
        "default_concurrency": DEFAULT_CONCURRENCY,
        "io_threads": IO_THREAD_POOL_SIZE,
    }


@app.on_event("startup")
async def on_startup():
    loop = asyncio.get_running_loop()
    loop.set_default_executor(GLOBAL_IO_EXECUTOR)
    load_tasks_index()
    asyncio.create_task(master_queue_scheduler())


# ==================== 公开业务 API ====================

@app.get("/api/overview")
async def api_overview():
    return {"ok": True, **get_public_overview()}


@app.post("/api/submit")
async def submit_batch(
    doi_file: Optional[UploadFile] = File(None),
    doi_text: Optional[str] = Form(None),
    concurrency: int = Form(DEFAULT_CONCURRENCY),
    naming_mode: str = Form("doi_title")
):
    raw_content = ""
    source_filename = "手动粘贴列表"
    if doi_file and doi_file.filename:
        raw_bytes = await doi_file.read()
        raw_content += raw_bytes.decode("utf-8", errors="ignore") + "\n"
        source_filename = doi_file.filename
    if doi_text:
        raw_content += doi_text

    dois = extract_dois_from_text(raw_content)
    if not dois:
        return JSONResponse({"ok": False, "error": "未在上传的文件或文本中识别到有效的 DOI（格式如 10.1038/nature12373）"}, status_code=400)

    safe_concurrency = max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, int(concurrency or DEFAULT_CONCURRENCY)))
    was_busy = (ACTIVE_TASK_ID is not None) or (len(PENDING_TASK_IDS) > 0)

    task_id = datetime.now().strftime("%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    items = [{
        "doi": d,
        "status": "pending",
        "title": "",
        "year": "",
        "author": "",
        "source": "",
        "filename": "",
        "size_kb": 0,
    } for d in dois]

    TASKS[task_id] = {
        "task_id": task_id,
        "title": source_filename,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": "",
        "finished_at": "",
        "status": "queued",
        "phase": "queued",
        "done_count": 0,
        "concurrency": safe_concurrency,
        "naming_mode": naming_mode,
        "total": len(dois),
        "current_doi": "",
        "success_count": 0,
        "failed_count": 0,
        "step_msg": "正在排队等待前序用户的任务完成..." if was_busy else f"任务已创建，即将开启 {min(safe_concurrency, len(dois))} 路并发检索开放文献库...",
        "zip_ready": False,
        "zip_dirty": True,
        "zip_path": "",
        "cancel_requested": False,
        "items": items
    }
    PENDING_TASK_IDS.append(task_id)
    save_tasks_index()
    q_info = compute_queue_info_for_task(task_id)
    QUEUE_EVENT.set()

    return {
        "ok": True,
        "task_id": task_id,
        "total": len(dois),
        "concurrency": safe_concurrency,
        "queued_behind_others": was_busy,
        "queue_info": q_info
    }


@app.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    q_info = compute_queue_info_for_task(task_id)
    return {
        "ok": True,
        "task": task,
        "queue_info": q_info,
        "overview": get_public_overview()
    }


@app.get("/api/tasks")
async def list_recent_tasks():
    summary = []
    for tid in sorted(TASKS.keys(), reverse=True)[:15]:
        t = TASKS[tid]
        summary.append({
            "task_id": t["task_id"],
            "title": t.get("title", "批量任务"),
            "created_at": t.get("created_at", ""),
            "status": t.get("status", ""),
            "phase": t.get("phase", ""),
            "total": t.get("total", 0),
            "success_count": t.get("success_count", 0),
            "failed_count": t.get("failed_count", 0),
            "concurrency": t.get("concurrency", DEFAULT_CONCURRENCY),
            "zip_ready": t.get("zip_ready", False),
            "queue_info": compute_queue_info_for_task(tid)
        })
    return {"ok": True, "tasks": summary, "overview": get_public_overview()}


@app.post("/api/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    task["cancel_requested"] = True
    if task_id in PENDING_TASK_IDS:
        PENDING_TASK_IDS.remove(task_id)
        task["status"] = "cancelled"
        task["step_msg"] = "已在排队阶段取消任务"
    else:
        task["step_msg"] = "正在停止任务并打包已下载文献..."
    async with get_task_zip_lock(task_id):
        await asyncio.to_thread(build_task_zip, task_id)
    save_tasks_index()
    return {"ok": True}


@app.post("/api/task/{task_id}/clear")
async def clear_single_task(task_id: str):
    if task_id in PENDING_TASK_IDS:
        PENDING_TASK_IDS.remove(task_id)
    task = TASKS.get(task_id)
    if task:
        task["cancel_requested"] = True
        task["cleared"] = True
        TASKS.pop(task_id, None)
    ZIP_BUILD_LOCKS.pop(task_id, None)
    task_dir = os.path.join(TASKS_DIR, os.path.basename(task_id))
    await asyncio.to_thread(shutil.rmtree, task_dir, True)
    save_tasks_index()
    return {"ok": True}


@app.post("/api/tasks/clear_all")
async def clear_all_tasks():
    PENDING_TASK_IDS.clear()
    for tid, t in list(TASKS.items()):
        t["cancel_requested"] = True
        t["cleared"] = True
    TASKS.clear()
    ZIP_BUILD_LOCKS.clear()
    for name in os.listdir(TASKS_DIR):
        p = os.path.join(TASKS_DIR, name)
        if os.path.isdir(p):
            await asyncio.to_thread(shutil.rmtree, p, True)
    save_tasks_index()
    return {"ok": True}


@app.get("/api/download/{task_id}")
async def download_zip(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    zip_path = task.get("zip_path")
    if task.get("zip_dirty") or not zip_path or not os.path.exists(zip_path):
        async with get_task_zip_lock(task_id):
            zip_path = await asyncio.to_thread(build_task_zip, task_id)
    if not zip_path or not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="压缩包尚未生成")
    dl_name = f"DOI_Papers_{task_id}.zip"
    return FileResponse(zip_path, media_type="application/zip", filename=dl_name)


@app.get("/api/download/{task_id}/file/{filename}")
async def download_single_pdf(task_id: str, filename: str):
    safe_name = os.path.basename(filename)
    fpath = os.path.join(TASKS_DIR, task_id, "pdfs", safe_name)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="PDF 文件不存在")
    return FileResponse(fpath, media_type="application/pdf", filename=safe_name)


@app.get("/api/download/{task_id}/failed_txt")
async def download_failed_dois_txt(task_id: str, include_unfetched: int = 0):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    lines = []
    for it in task.get("items", []):
        if it.get("status") == "failed" or (include_unfetched and it.get("status") != "success"):
            lines.append(it["doi"])
    txt_bytes = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    headers = {"Content-Disposition": f'attachment; filename="Failed_DOIs_{task_id}.txt"'}
    return Response(content=txt_bytes, media_type="text/plain; charset=utf-8", headers=headers)


# ==================== 前端界面 (Light Liquid Glass 风格 · 纯开放文献库高并发版) ====================

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <meta name="theme-color" content="#f4f7fb">
  <title>文献批量获取中心 · Open-Source High-Concurrency DOI Downloader</title>
  <link rel="icon" href="data:,">
  <style>
    :root {
      color-scheme: light;
      --ink: #18263c;
      --secondary: #586b82;
      --muted: #61728a;
      --blue: #0878ed;
      --green: #168452;
      --orange: #aa650b;
      --red: #cf4454;
      --line: rgba(93, 119, 151, .13);
      --spring: cubic-bezier(.2, .8, .2, 1);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif;
      font-synthesis: none;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body { margin: 0; min-width: 320px; color: var(--ink); background: #f4f7fb; padding-bottom: 60px; }
    body::before {
      content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
      background:
        radial-gradient(ellipse at 9% 6%, rgba(187, 216, 252, .52), transparent 41%),
        radial-gradient(ellipse at 92% 28%, rgba(198, 232, 235, .44), transparent 38%),
        radial-gradient(ellipse at 52% 100%, rgba(214, 223, 244, .45), transparent 44%),
        linear-gradient(140deg, #f7f9fc, #eff5fa 55%, #f7f8fb);
    }
    button, input, select, textarea { font: inherit; color: inherit; }
    button { cursor: pointer; -webkit-tap-highlight-color: transparent; }
    button:disabled { cursor: not-allowed; opacity: .55; }
    .icon { width: 19px; height: 19px; fill: none; stroke: currentColor; stroke-width: 1.75; stroke-linecap: round; stroke-linejoin: round; flex: 0 0 auto; }

    /* Light Liquid Glass 材质 */
    .liquid {
      position: relative; isolation: isolate;
      border: 1px solid rgba(255, 255, 255, .95);
      box-shadow: 0 8px 30px rgba(55, 83, 123, .07), 0 1px 3px rgba(55, 83, 123, .04), inset 0 1px 0 #fff, inset 0 -1px 0 rgba(255, 255, 255, .7);
    }
    .liquid::before {
      content: ""; position: absolute; inset: 0; z-index: -1; border-radius: inherit; pointer-events: none;
      background: rgba(255, 255, 255, .58);
      -webkit-backdrop-filter: blur(22px) saturate(165%);
      backdrop-filter: blur(22px) saturate(165%);
    }
    .liquid::after {
      content: ""; position: absolute; inset: 0; z-index: -1; border-radius: inherit; pointer-events: none;
      background: radial-gradient(ellipse at var(--glass-x, 25%) var(--glass-y, 0%), rgba(255, 255, 255, .72), transparent 62%);
      box-shadow: inset 1px 1px 1px rgba(255, 255, 255, .92), inset -1px -1px 1px rgba(134, 171, 211, .09);
    }
    html[data-optics="chromium"] .liquid::before { backdrop-filter: url(#glass-refraction) blur(18px) saturate(165%); }

    .shell { width: min(1240px, calc(100% - 64px)); margin-inline: auto; }
    @media (max-width: 760px) { .shell { width: calc(100% - 28px); } }

    .topbar {
      position: sticky; top: 18px; z-index: 30; margin-top: 22px; min-height: 68px;
      padding: 10px 18px;
      display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;
      border-radius: 23px;
    }
    .brand-wrap { display: flex; align-items: center; gap: 11px; flex: 1 1 340px; min-width: 0; }
    .brand-badge {
      width: 38px; height: 38px; border-radius: 12px;
      background: linear-gradient(135deg, #0878ed, #38bdf8);
      color: #fff; display: grid; place-items: center;
      box-shadow: 0 4px 14px rgba(8, 120, 237, .25);
      flex-shrink: 0;
    }
    .console-title { font-size: 16px; font-weight: 650; letter-spacing: -.3px; line-height: 1.3; }
    .console-sub { font-size: 11px; color: var(--muted); margin-top: 2px; line-height: 1.35; }
    .topbar-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

    .node-badge {
      display: inline-flex; align-items: center; gap: 7px;
      padding: 7px 13px; border-radius: 999px;
      border: 1px solid rgba(122, 152, 189, .18);
      background: rgba(255, 255, 255, .76);
      font-size: 12px; font-weight: 600; color: var(--secondary);
    }
    .node-badge.online { color: var(--green); background: #eef8f2; border-color: #d6efe0; }
    .node-badge.busy { color: var(--blue); background: #edf5fe; border-color: #cfe3fc; }
    .status-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex: 0 0 auto; }

    .btn-lang {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 6px 14px; border-radius: 999px;
      border: 1px solid rgba(8, 120, 237, .28);
      background: rgba(255, 255, 255, .92);
      color: var(--blue); font-size: 12.5px; font-weight: 650;
      box-shadow: 0 2px 8px rgba(8, 120, 237, .1);
      transition: all .2s var(--spring);
      cursor: pointer;
    }
    .btn-lang:hover {
      background: #fff;
      transform: translateY(-1px);
      box-shadow: 0 4px 14px rgba(8, 120, 237, .2);
      border-color: var(--blue);
    }

    main { padding-top: 38px; }
    .page-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 20px; margin-bottom: 24px; padding-inline: 4px; flex-wrap: wrap; }
    .eyebrow { display: flex; align-items: center; gap: 8px; margin: 0 0 10px; color: #596e88; font-size: 11px; font-weight: 600; letter-spacing: 2px; text-transform: uppercase; }
    .eyebrow::before { content: ""; width: 16px; height: 1px; background: #8aa4c3; }
    h1 { margin: 0; font-size: clamp(26px, 3.2vw, 38px); font-weight: 650; letter-spacing: -1.2px; line-height: 1.25; }
    .page-description { margin: 10px 0 0; color: var(--secondary); font-size: 14px; line-height: 1.65; max-width: 820px; }
    .date-block { text-align: right; color: var(--secondary); font-size: 13px; white-space: nowrap; }
    .date-block time { display: block; color: #34465e; font-weight: 550; margin-bottom: 5px; font-variant-numeric: tabular-nums; }
    .date-block span { font-size: 12px; color: var(--muted); }

    .queue-banner {
      display: flex; align-items: flex-start; gap: 13px;
      margin-bottom: 18px; padding: 15px 20px;
      border-radius: 20px;
      border: 1px solid #efdfb9;
      background: rgba(255, 251, 235, .88);
      color: #875e22;
      box-shadow: 0 6px 20px rgba(170, 101, 11, .06);
      font-size: 13.5px; line-height: 1.65;
    }
    .queue-banner.idle { border-color: #d6efe0; background: rgba(238, 248, 242, .82); color: #166e43; }
    .queue-banner-icon {
      width: 34px; height: 34px; border-radius: 11px;
      background: rgba(255, 255, 255, .85);
      display: grid; place-items: center; flex: 0 0 auto; margin-top: 1px;
    }

    .overview {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      border-radius: 27px; background: rgba(255, 255, 255, .76);
      border: 1px solid #fff; box-shadow: 0 6px 24px rgba(67, 92, 125, .04);
      margin-bottom: 26px;
    }
    .overview-item { position: relative; min-width: 0; padding: 20px 22px; }
    .overview-item + .overview-item::before { content: ""; position: absolute; left: 0; top: 20px; bottom: 20px; width: 1px; background: var(--line); }
    @media (max-width: 760px) {
      .overview-item + .overview-item::before { display: none; }
      .overview-item { border-bottom: 1px solid var(--line); }
      .overview-item:last-child { border-bottom: none; }
    }
    .overview-label { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--secondary); font-size: 13px; font-weight: 550; }
    .stat-icon { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 9px; color: #317bca; background: #edf4fc; flex-shrink: 0; }
    .stat-icon.green { color: #288462; background: #edf7f1; }
    .stat-icon.teal { color: #238b99; background: #edf7f8; }
    .stat-icon.slate { color: #6c7fab; background: #f0f2f8; }
    .overview-value { margin-top: 11px; height: 38px; display: flex; align-items: baseline; gap: 5px; font-size: 27px; font-weight: 600; line-height: 1.15; letter-spacing: -1px; font-variant-numeric: tabular-nums; }
    .overview-value .unit { margin-left: 2px; font-size: 13.5px; color: var(--secondary); font-weight: 500; letter-spacing: 0; }
    .overview-value .slash { margin-left: 3px; font-size: 15px; font-weight: 400; color: #66788e; }
    .overview-note { display: flex; align-items: center; gap: 6px; margin-top: 8px; min-height: 18px; color: var(--secondary); font-size: 12px; line-height: 1.5; flex-wrap: wrap; }

    .workspace-grid { display: grid; grid-template-columns: minmax(360px, 440px) minmax(0, 1fr); gap: 20px; align-items: start; }
    @media (max-width: 980px) { .workspace-grid { grid-template-columns: 1fr; } }

    .glass-panel { border-radius: 25px; padding: 24px; background: rgba(255, 255, 255, .82); }
    .panel-header { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 18px; flex-wrap: wrap; }
    .panel-header h2 { margin: 0; font-size: 18px; font-weight: 620; letter-spacing: -.4px; }
    .panel-sub { font-size: 12px; color: var(--muted); margin-top: 3px; }

    .drop-zone {
      border: 1.5px dashed #9fc3e8; border-radius: 18px; padding: 22px 16px;
      text-align: center; background: rgba(240, 246, 253, .65);
      cursor: pointer; transition: all .25s var(--spring); margin-bottom: 16px;
    }
    .drop-zone:hover, .drop-zone.dragover { border-color: var(--blue); background: rgba(230, 242, 255, .85); transform: translateY(-1px); }
    .drop-zone input { display: none; }
    .drop-icon {
      width: 42px; height: 42px; border-radius: 14px; background: #fff; color: var(--blue);
      display: grid; place-items: center; margin: 0 auto 10px; box-shadow: 0 4px 12px rgba(8, 120, 237, .1);
    }
    .drop-title { font-size: 14px; font-weight: 600; color: var(--ink); }
    .drop-hint { font-size: 12px; color: var(--muted); margin-top: 4px; }
    .selected-file-pill {
      display: inline-flex; align-items: center; gap: 6px; margin-top: 10px; padding: 5px 12px;
      border-radius: 999px; background: #eef8f2; color: var(--green); border: 1px solid #d6efe0; font-size: 12px; font-weight: 600;
    }

    .field-group { margin-bottom: 14px; }
    .field-label { display: block; font-size: 12.5px; font-weight: 600; color: #34465e; margin-bottom: 6px; line-height: 1.45; }
    textarea, select {
      width: 100%; border: 1px solid rgba(138, 164, 195, .32); border-radius: 14px;
      background: rgba(255, 255, 255, .92); padding: 10px 13px; font-size: 13px; color: var(--ink); outline: none;
    }
    textarea { height: 145px; resize: vertical; font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace; font-size: 12.5px; line-height: 1.55; }
    textarea:focus, select:focus { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(8, 120, 237, .12); }

    .btn-primary {
      width: 100%; min-height: 44px; border: 1px solid rgba(255, 255, 255, .4); border-radius: 15px;
      background: linear-gradient(135deg, #0878ed, #2196f3); color: #fff; font-size: 14px; font-weight: 600;
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      box-shadow: 0 6px 18px rgba(8, 120, 237, .24), inset 0 1px 0 rgba(255,255,255,.35);
      transition: transform .2s var(--spring), box-shadow .2s; text-decoration: none;
    }
    .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 9px 22px rgba(8, 120, 237, .3); }
    .btn-green { background: linear-gradient(135deg, #168452, #20a066); box-shadow: 0 6px 18px rgba(22, 132, 82, .24), inset 0 1px 0 rgba(255,255,255,.35); }
    .btn-soft {
      display: inline-flex; align-items: center; gap: 6px; padding: 7px 13px; border-radius: 12px;
      border: 1px solid rgba(122, 152, 189, .24); background: rgba(255, 255, 255, .85);
      color: #2c435f; font-size: 12.5px; font-weight: 550; text-decoration: none;
    }

    .status-box {
      border-radius: 20px; padding: 18px 20px; background: rgba(244, 248, 253, .85);
      border: 1px solid #fff; box-shadow: inset 0 1px 0 #fff, 0 2px 10px rgba(53, 84, 126, .03); margin-bottom: 18px;
    }
    .meter-track { height: 7px; border-radius: 999px; overflow: hidden; background: #e3ebf4; margin: 12px 0; }
    .meter-fill { width: 0%; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #0878ed, #28a76b); transition: width .4s var(--spring); }

    .my-queue-alert {
      margin-bottom: 16px; padding: 16px 18px; border-radius: 18px;
      background: linear-gradient(135deg, rgba(255, 249, 232, .95), rgba(255, 244, 214, .92));
      border: 1px solid #f2d99b; color: #7d5211; font-size: 13px; line-height: 1.65;
    }

    .table-surface {
      border: 1px solid #fff; border-radius: 20px; background: rgba(255, 255, 255, .88);
      overflow: hidden; box-shadow: 0 4px 16px rgba(53, 84, 126, .035);
    }
    .table-scroll { max-height: 430px; overflow-y: auto; overflow-x: auto; }
    table { width: 100%; min-width: 580px; border-collapse: collapse; text-align: left; font-size: 13px; font-variant-numeric: tabular-nums; }
    th {
      position: sticky; top: 0; z-index: 2; padding: 13px 15px; border-bottom: 1px solid var(--line);
      color: var(--secondary); font-size: 12px; font-weight: 550; background: rgba(247, 249, 252, .95); backdrop-filter: blur(8px);
      white-space: nowrap;
    }
    td { padding: 13px 15px; border-bottom: 1px solid var(--line); vertical-align: middle; }
    tbody tr:last-child td { border-bottom: 0; }
    tbody tr:hover { background: #f5f9fd; }

    .pill-status {
      display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px;
      border-radius: 999px; font-size: 11.5px; font-weight: 600; white-space: nowrap; text-decoration: none;
    }
    .pill-pending { color: #61728a; background: #eef2f7; border: 1px solid #e1e8f0; }
    .pill-fetching { color: var(--blue); background: #edf5fe; border: 1px solid #d5e7fc; }
    .pill-success { color: var(--green); background: #eef8f2; border: 1px solid #daefe2; }
    .pill-failed { color: var(--red); background: #fdf0f1; border: 1px solid #f9d8dc; }

    .history-grid { display: grid; gap: 9px; margin-top: 12px; }
    .history-row {
      display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
      padding: 12px 16px; border-radius: 16px; border: 1px solid #fff; background: rgba(255, 255, 255, .75);
      box-shadow: 0 2px 8px rgba(53, 84, 126, .03); cursor: pointer;
    }
    .history-row:hover { background: #fff; }
    .footer { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; padding: 28px 4px 0; color: var(--muted); font-size: 12px; }
  </style>
</head>
<body>
  <svg width="0" height="0" aria-hidden="true" style="position:absolute;pointer-events:none">
    <defs>
      <filter id="glass-refraction" x="-10%" y="-10%" width="120%" height="120%" color-interpolation-filters="sRGB">
        <feTurbulence type="fractalNoise" baseFrequency=".009 .014" numOctaves="2" seed="9" result="noise"/>
        <feGaussianBlur in="noise" stdDeviation="2" result="soft-noise"/>
        <feDisplacementMap in="SourceGraphic" in2="soft-noise" scale="7" xChannelSelector="R" yChannelSelector="G"/>
      </filter>
      <symbol id="i-book" viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></symbol>
      <symbol id="i-upload" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></symbol>
      <symbol id="i-download" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></symbol>
      <symbol id="i-users" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></symbol>
      <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
      <symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 3 4 6v5c0 5 3 8 8 10 5-2 8-5 8-10V6z"/><path d="m8.5 12 2.5 2.5 4.5-5"/></symbol>
      <symbol id="i-zap" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></symbol>
    </defs>
  </svg>

  <!-- 顶部导航栏 -->
  <header class="topbar shell liquid">
    <div class="brand-wrap">
      <div class="brand-badge"><svg class="icon"><use href="#i-book"/></svg></div>
      <div>
        <div class="console-title" data-i18n="topbar_title">文献批量抓取中心 · 开放文献库高并发下载引擎</div>
        <div class="console-sub" data-i18n="topbar_sub">Sci-Hub 四镜像池 + Unpaywall + OpenAlex + EuropePMC + SemanticScholar + 出版商 OA 直链</div>
      </div>
    </div>
    <div class="topbar-right">
      <div class="node-badge online">
        <span class="status-dot"></span>
        <span id="matrix-badge-text" data-i18n="badge_sources">开放文献库矩阵: 6大源在线</span>
      </div>
      <div class="node-badge busy" id="pill-concurrency">
        <span class="status-dot"></span>
        <span id="pill-concurrency-text">⚡ 高并发引擎: 16路就绪 (2C/12G)</span>
      </div>
      <button type="button" class="btn-lang" id="sci-lang-btn" onclick="toggleSciLang()" title="Switch Language / 切换语言">🌐 EN</button>
    </div>
  </header>

  <main class="shell">
    <div class="page-heading">
      <div>
        <p class="eyebrow">HIGH-CONCURRENCY OPEN-SOURCE LITERATURE PIPELINE</p>
        <h1 data-i18n="page_h1">批量 DOI 文献获取</h1>
        <p class="page-description" data-i18n="page_desc">上传每行一个 DOI 的文件或直接粘贴 DOI 列表即可。系统基于 2 OCPU / 12GB RAM 大带宽实例开启多路并发，同步检索 Sci-Hub 多镜像池、Unpaywall 知识库及四大 OA 库，自动打包成 ZIP。</p>
      </div>
      <div class="date-block">
        <time id="today-time">—</time>
        <span data-i18n="date_sub">北京时间 · UTC+8 · 高并发自动队列</span>
      </div>
    </div>

    <!-- 全局系统空闲/排队提醒横幅 -->
    <div class="queue-banner idle liquid" id="global-queue-banner">
      <div class="queue-banner-icon"><svg class="icon"><use href="#i-shield"/></svg></div>
      <div id="global-queue-text">
        <strong>🟢 系统当前空闲就绪</strong> — 上传或粘贴 DOI 列表后将立即开启多路并发检索全量开放文献库，处理完毕后自动返回打包好的 PDF 压缩包。
      </div>
    </div>

    <!-- 四栏概览面板 -->
    <section class="overview liquid" aria-label="系统实时运行概览">
      <div class="overview-item">
        <div class="overview-label"><span data-i18n="ov_matrix_lbl">开放检索矩阵</span><span class="stat-icon green"><svg class="icon"><use href="#i-users"/></svg></span></div>
        <div class="overview-value"><span>6</span><span class="slash" data-i18n="ov_matrix_unit">大开放库</span></div>
        <div class="overview-note">Sci-Hub + Unpaywall + OpenAlex + PMC 等</div>
      </div>
      <div class="overview-item">
        <div class="overview-label"><span data-i18n="ov_queue_lbl">系统排队状态</span><span class="stat-icon"><svg class="icon"><use href="#i-clock"/></svg></span></div>
        <div class="overview-value"><span id="ov-queue-state">空闲</span></div>
        <div class="overview-note" id="ov-queue-note">当前无等待中的排队任务</div>
      </div>
      <div class="overview-item">
        <div class="overview-label"><span data-i18n="ov_progress_lbl">当前批次进度</span><span class="stat-icon teal"><svg class="icon"><use href="#i-book"/></svg></span></div>
        <div class="overview-value"><span id="ov-cur-done">0</span><span class="slash" id="ov-cur-total">/ 0 篇</span></div>
        <div class="overview-note" id="ov-cur-note">成功 0 篇 · 未命中 0 篇</div>
      </div>
      <div class="overview-item">
        <div class="overview-label"><span data-i18n="ov_engine_lbl">并行加速规格</span><span class="stat-icon slate"><svg class="icon"><use href="#i-zap"/></svg></span></div>
        <div class="overview-value"><span id="ov-concurrency">16</span><span class="unit" data-i18n="ov_engine_unit">路并发</span></div>
        <div class="overview-note" id="ov-concurrency-note">ARM64 2C/12G · 64线程异步I/O池</div>
      </div>
    </section>

    <div class="workspace-grid">
      <!-- 左侧：上传与参数 -->
      <section class="glass-panel liquid">
        <div class="panel-header">
          <div>
            <h2 data-i18n="p1_title">1. 输入或上传 DOI</h2>
            <div class="panel-sub" data-i18n="p1_sub">支持拖拽上传文本文件，或直接按行粘贴 DOI 列表</div>
          </div>
          <button type="button" class="btn-soft" onclick="clearInputsOnly()" style="padding:5px 11px; font-size:12px;" data-i18n="btn_clear_input">
            🧹 清空输入
          </button>
        </div>

        <div class="drop-zone" id="drop-zone" onclick="document.getElementById('doi-file').click()">
          <input type="file" id="doi-file" accept=".txt,.csv,.tsv,.bib,.ris,.md" onchange="onFileSelected(this)">
          <div class="drop-icon"><svg class="icon"><use href="#i-upload"/></svg></div>
          <div class="drop-title" data-i18n="drop_title">点击选择 DOI 文件 或 将文件拖拽至此</div>
          <div class="drop-hint" data-i18n="drop_hint">支持 .txt / .csv / .bib，自动按行提取 10.xxxx/... 格式 DOI</div>
          <div id="selected-file-box" hidden>
            <span class="selected-file-pill" id="selected-filename"></span>
          </div>
        </div>

        <div class="field-group">
          <label class="field-label" for="doi-text" data-i18n="lbl_doi_text">或者直接按行粘贴 DOI 列表：</label>
          <textarea id="doi-text" placeholder="10.1038/nature12373&#10;10.1126/science.169.3946.635&#10;10.1038/s41586-020-2649-2"></textarea>
        </div>

        <div class="field-group">
          <label class="field-label" for="concurrency-select" data-i18n="lbl_concurrency">并行下载通道数（已按 2 OCPU / 12GB VPS 硬件调优）：</label>
          <select id="concurrency-select" onchange="document.getElementById('ov-concurrency').textContent=this.value">
            <option value="16" selected data-i18n="opt_conc_16">🚀 16 路高速并发（默认推荐 · 2C/12G 大带宽黄金配比）</option>
            <option value="24" data-i18n="opt_conc_24">⚡ 24 路极速并发（适合 50+ 篇大批量 DOI 快速扫库）</option>
            <option value="12" data-i18n="opt_conc_12">🟢 12 路均衡并发（兼顾高速抓取与外部 API 礼貌限流）</option>
            <option value="8" data-i18n="opt_conc_8">🛡️ 8 路稳健并发（适合少量文献检索）</option>
          </select>
        </div>

        <div class="field-group">
          <label class="field-label" for="naming-mode" data-i18n="lbl_naming">压缩包内 PDF 命名格式：</label>
          <select id="naming-mode">
            <option value="doi_title" selected data-i18n="opt_name_title">📑 年份_第一作者_文献标题 (DOI).pdf（自动补全论文标题）</option>
            <option value="doi_only" data-i18n="opt_name_doi">🔢 纯 DOI 号命名 (如 10.1038_nature12373.pdf)</option>
          </select>
        </div>

        <button type="button" class="btn-primary" id="submit-btn" onclick="submitBatch()">
          <svg class="icon"><use href="#i-upload"/></svg>
          <span data-i18n="btn_submit">开始高并发批量下载与打包</span>
        </button>
      </section>

      <!-- 右侧：实时处理监控与 ZIP 打包输出 -->
      <section class="glass-panel liquid">
        <div class="panel-header" style="flex-wrap:wrap;">
          <div>
            <h2 data-i18n="p2_title">2. 实时进度与压缩包下载</h2>
            <div class="panel-sub" data-i18n="p2_sub">多路并发同步检索开放文献库，支持单篇即下与全量 ZIP 归档下载</div>
          </div>
          <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
            <button type="button" class="btn-soft" id="export-failed-btn" hidden onclick="scrollToFailedWhiteboard()" style="color: var(--red); border-color:#f7d6da; background:#fff;">
              <span data-i18n="btn_export_failed">📋 导出失败 DOI</span> (<span id="export-failed-btn-cnt">0</span>)
            </button>
            <button type="button" class="btn-soft" id="stop-btn" hidden onclick="cancelCurrentTask()" style="color: var(--orange); border-color:#f3e2c3; background:#fcf5ea;" data-i18n="btn_stop_pack">
              ⏹️ 提前结束并打包
            </button>
            <button type="button" class="btn-soft" id="reset-btn" hidden onclick="clearAndResetCurrentTask()" style="color: var(--red); border-color:#f9d8dc; background:#fdf0f1;" data-i18n="btn_reset_all">
              🗑️ 一键清空记录复原
            </button>
          </div>
        </div>

        <div class="my-queue-alert" id="my-queue-alert" hidden></div>

        <div class="status-box">
          <div style="display:flex; justify-content:space-between; align-items:center; gap:10px;">
            <span id="step-msg" style="font-size:13.5px; font-weight:600; color:var(--ink);">等待上传文件或粘贴 DOI 列表...</span>
            <span id="pct-text" style="font-size:13.5px; font-weight:650; color:var(--blue); font-variant-numeric:tabular-nums;">0%</span>
          </div>
          <div class="meter-track"><div class="meter-fill" id="progress-fill"></div></div>
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; font-size:12px; color:var(--secondary);">
            <span><span data-i18n="lbl_task_id">任务编号:</span> <strong id="st-id" style="color:var(--ink);">-</strong></span>
            <span><span data-i18n="lbl_task_conc">并行通道:</span> <strong id="st-concurrency" style="color:var(--green);">16 路并发就绪</strong></span>
            <span><span data-i18n="lbl_task_scope">检索范围:</span> <strong style="color:var(--blue);">Sci-Hub 4镜像 + Unpaywall + OpenAlex + PMC</strong></span>
          </div>

          <div id="zip-download-box" hidden style="margin-top: 15px; display:grid; gap:9px;">
            <a id="zip-download-link" href="#" class="btn-primary btn-green">
              <svg class="icon"><use href="#i-download"/></svg>
              <span id="zip-download-text">一键下载全部文献压缩包 (.ZIP)</span>
            </a>
            <button type="button" class="btn-soft" onclick="clearAndResetCurrentTask()" style="width:100%; justify-content:center; color:var(--secondary); border-color:#dce6f2; padding:9px 14px;" data-i18n="btn_zip_reset_hint">
              🧹 已下载完毕 / 不想继续当前任务？点击一键清除记录并复原界面
            </button>
          </div>
        </div>

        <div class="table-surface">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th style="width:48px;" data-i18n="th_idx">序号</th>
                  <th data-i18n="th_doi">DOI / 自动解析文献标题</th>
                  <th style="width:185px;" data-i18n="th_source">命中开放库渠道</th>
                  <th style="width:130px;" data-i18n="th_status">状态 / 单篇下载</th>
                </tr>
              </thead>
              <tbody id="items-tbody">
                <tr><td colspan="4" style="text-align:center; padding:42px 16px; color:var(--muted);" data-i18n="tbl_empty">上传或粘贴 DOI 列表后，这里将实时显示每篇文献的并发抓取进度与下载按钮</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- 下载失败 / 未命中 DOI 导出白板 -->
        <div id="failed-whiteboard-box" hidden style="margin-top: 18px; padding: 18px 20px; border-radius: 20px; background: rgba(253, 244, 245, .72); border: 1px solid #f7d6da; box-shadow: inset 0 1px 0 #fff, 0 4px 14px rgba(207, 68, 84, .05);">
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:10px;">
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
              <span style="font-size:14px; font-weight:650; color:var(--red);" data-i18n="wb_title">📋 下载失败（未命中）DOI 导出白板</span>
              <span class="pill-status pill-failed" id="wb-failed-count">0 篇</span>
              <span id="wb-copy-toast" style="font-size:12px; font-weight:600; color:var(--green);" hidden>✓ 已复制到剪贴板！</span>
            </div>
            <div style="display:flex; align-items:center; gap:7px; flex-wrap:wrap;">
              <select id="wb-format-mode" onchange="refreshFailedWhiteboardText(true)" style="width:auto; padding:5px 10px; font-size:12px; border-radius:10px; height:auto;">
                <option value="pure_failed" selected data-i18n="opt_wb_pure">仅下载失败 DOI（每行一个）</option>
                <option value="all_unfetched" data-i18n="opt_wb_all">全部未成功 DOI（含失败 + 未跑完）</option>
                <option value="with_title" data-i18n="opt_wb_title">失败 DOI + 年份与标题注释</option>
              </select>
              <button type="button" class="btn-soft" onclick="copyFailedWhiteboard()" style="padding:6px 12px; font-size:12px; color:var(--blue); border-color:#cfe3fc; background:#fff; font-weight:600;" data-i18n="btn_wb_copy">
                📑 一键复制全部
              </button>
              <button type="button" class="btn-soft" onclick="downloadFailedWhiteboardTxt()" style="padding:6px 12px; font-size:12px; color:var(--green); border-color:#cdebd8; background:#fff; font-weight:600;" data-i18n="btn_wb_download">
                💾 下载 .txt 格式
              </button>
              <button type="button" class="btn-soft" onclick="refillFailedToLeftInput()" style="padding:6px 12px; font-size:12px; color:var(--secondary); background:#fff;" data-i18n="btn_wb_refill">
                🔄 回填至左侧重试
              </button>
            </div>
          </div>
          <textarea id="failed-whiteboard-text" spellcheck="false" placeholder="暂无下载失败的 DOI..." style="height:135px; background:rgba(255,255,255,.96); border-color:#f1c8cd; font-family:'SF Mono','JetBrains Mono',Consolas,monospace; font-size:12.5px; line-height:1.6;"></textarea>
          <div style="font-size:11.5px; color:var(--muted); margin-top:6px;" data-i18n="wb_hint">
            💡 提示：白板内的 DOI 支持直接鼠标框选复制粘贴，或点击上方「📑 一键复制全部」与「💾 下载 .txt 格式」保存。
          </div>
        </div>

        <div style="margin-top: 22px;">
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
            <span style="font-size:13.5px; font-weight:620; color:var(--secondary);" data-i18n="hist_title">🕒 任务队列与最近完成批次（点击可切换查看）</span>
            <div style="display:flex; align-items:center; gap:8px;">
              <button type="button" class="btn-soft" style="padding:4px 10px; font-size:11.5px;" onclick="loadHistory()" data-i18n="btn_hist_refresh">刷新列表</button>
              <button type="button" class="btn-soft" style="padding:4px 10px; font-size:11.5px; color:var(--red); border-color:#f9d8dc;" onclick="clearAllHistoryTasks()" data-i18n="btn_hist_clear">🗑️ 清空全部记录</button>
            </div>
          </div>
          <div class="history-grid" id="history-list"></div>
        </div>
      </section>
    </div>

    <footer class="footer">
      <span data-i18n="footer_left">文献批量抓取与打包平台 · Open-Source High-Concurrency Edition (v6.0)</span>
      <span data-i18n="footer_right">Oracle Cloud Ampere ARM64 (2 OCPU / 12GB RAM / 2Gbps) 算力驱动</span>
    </footer>
  </main>

  <script>
    if (/Chrome|Edg|Brave/.test(navigator.userAgent)) {
      document.documentElement.dataset.optics = "chromium";
    }
    document.addEventListener("pointermove", e => {
      const target = e.target.closest(".liquid");
      if (!target) return;
      const rect = target.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / Math.max(rect.width, 1)) * 100;
      const y = ((e.clientY - rect.top) / Math.max(rect.height, 1)) * 100;
      target.style.setProperty("--glass-x", x.toFixed(1) + "%");
      target.style.setProperty("--glass-y", y.toFixed(1) + "%");
    }, { passive: true });

    // ==========================================
    // 中英双语国际化 (Bilingual i18n System)
    // ==========================================
    const SCI_I18N = {
      zh: {
        page_title: "文献批量获取中心 · Open-Source High-Concurrency DOI Downloader",
        topbar_title: "文献批量抓取中心 · 开放文献库高并发下载引擎",
        topbar_sub: "Sci-Hub 四镜像池 + Unpaywall + OpenAlex + EuropePMC + SemanticScholar + 出版商 OA 直链",
        badge_sources: "开放文献库矩阵: 6大源在线",
        page_h1: "批量 DOI 文献获取",
        page_desc: "上传每行一个 DOI 的文件或直接粘贴 DOI 列表即可。系统基于 2 OCPU / 12GB RAM 大带宽实例开启多路并发，同步检索 Sci-Hub 多镜像池、Unpaywall 知识库及四大 OA 库，自动打包成 ZIP。",
        date_sub: "北京时间 · UTC+8 · 高并发自动队列",
        ov_matrix_lbl: "开放检索矩阵",
        ov_matrix_unit: "大开放库",
        ov_queue_lbl: "系统排队状态",
        ov_progress_lbl: "当前批次进度",
        ov_engine_lbl: "并行加速规格",
        ov_engine_unit: "路并发",
        p1_title: "1. 输入或上传 DOI",
        p1_sub: "支持拖拽上传文本文件，或直接按行粘贴 DOI 列表",
        btn_clear_input: "🧹 清空输入",
        drop_title: "点击选择 DOI 文件 或 将文件拖拽至此",
        drop_hint: "支持 .txt / .csv / .bib，自动按行提取 10.xxxx/... 格式 DOI",
        lbl_doi_text: "或者直接按行粘贴 DOI 列表：",
        lbl_concurrency: "并行下载通道数（已按 2 OCPU / 12GB VPS 硬件调优）：",
        opt_conc_16: "🚀 16 路高速并发（默认推荐 · 2C/12G 大带宽黄金配比）",
        opt_conc_24: "⚡ 24 路极速并发（适合 50+ 篇大批量 DOI 快速扫库）",
        opt_conc_12: "🟢 12 路均衡并发（兼顾高速抓取与外部 API 礼貌限流）",
        opt_conc_8: "🛡️ 8 路稳健并发（适合少量文献检索）",
        lbl_naming: "压缩包内 PDF 命名格式：",
        opt_name_title: "📑 年份_第一作者_文献标题 (DOI).pdf（自动补全论文标题）",
        opt_name_doi: "🔢 纯 DOI 号命名 (如 10.1038_nature12373.pdf)",
        btn_submit: "开始高并发批量下载与打包",
        p2_title: "2. 实时进度与压缩包下载",
        p2_sub: "多路并发同步检索开放文献库，支持单篇即下与全量 ZIP 归档下载",
        btn_export_failed: "📋 导出失败 DOI",
        btn_stop_pack: "⏹️ 提前结束并打包",
        btn_reset_all: "🗑️ 一键清空记录复原",
        lbl_task_id: "任务编号:",
        lbl_task_conc: "并行通道:",
        lbl_task_scope: "检索范围:",
        btn_zip_reset_hint: "🧹 已下载完毕 / 不想继续当前任务？点击一键清除记录并复原界面",
        th_idx: "序号",
        th_doi: "DOI / 自动解析文献标题",
        th_source: "命中开放库渠道",
        th_status: "状态 / 单篇下载",
        tbl_empty: "上传或粘贴 DOI 列表后，这里将实时显示每篇文献的并发抓取进度与下载按钮",
        wb_title: "📋 下载失败（未命中）DOI 导出白板",
        opt_wb_pure: "仅下载失败 DOI（每行一个）",
        opt_wb_all: "全部未成功 DOI（含失败 + 未跑完）",
        opt_wb_title: "失败 DOI + 年份与标题注释",
        btn_wb_copy: "📑 一键复制全部",
        btn_wb_download: "💾 下载 .txt 格式",
        btn_wb_refill: "🔄 回填至左侧重试",
        wb_hint: "💡 提示：白板内的 DOI 支持直接鼠标框选复制粘贴，或点击上方「📑 一键复制全部」与「💾 下载 .txt 格式」保存。",
        hist_title: "🕒 任务队列与最近完成批次（点击可切换查看）",
        btn_hist_refresh: "刷新列表",
        btn_hist_clear: "🗑️ 清空全部记录",
        footer_left: "文献批量抓取与打包平台 · Open-Source High-Concurrency Edition (v6.0)",
        footer_right: "Oracle Cloud Ampere ARM64 (2 OCPU / 12GB RAM / 2Gbps) 算力驱动",
        waiting_input: "等待上传文件或粘贴 DOI 列表...",
        queue_idle_title: "🟢 系统当前空闲就绪",
        queue_idle_desc: "上传或粘贴每行一个 DOI 的列表后，系统将开启多路高并发，同步检索全套开放文献库（Sci-Hub 四镜像池 + Unpaywall + OpenAlex + SemanticScholar + EuropePMC）并自动打包成 ZIP。",
        queue_busy_other_title: "⏳ 其他用户正在使用系统",
        queue_busy_mine_title: "🚀 您的任务正在高速执行中",
        state_idle: "空闲",
        state_running: "运行中",
        state_queued: "排队中",
        note_queue_idle: "队列无积压 · 提交后立即开始",
        status_pending: "待检索",
        status_fetching: "⚡ 并发检索中...",
        status_download: "✓ 下载",
        status_failed: "未命中",
        status_completed: "已完成",
        status_stopped: "已停止",
        status_interrupted: "已中断",
        btn_hist_dl: "📦 下载 ZIP",
        btn_hist_del: "✕ 清除",
        no_history: "暂无历史批次记录"
      },
      en: {
        page_title: "DOI Retrieval Hub · High-Concurrency Open Literature Engine",
        topbar_title: "DOI Retrieval Hub · High-Concurrency Literature Engine",
        topbar_sub: "Sci-Hub 4 Mirrors + Unpaywall + OpenAlex + EuropePMC + SemanticScholar + Publisher OA",
        badge_sources: "Literature Matrix: 6 Sources Online",
        page_h1: "Batch DOI Literature Retrieval",
        page_desc: "Upload a DOI list or paste lines of DOIs directly. Powered by 2 OCPU / 12GB RAM high-bandwidth compute, the system concurrently queries Sci-Hub mirror pools, Unpaywall, and major OA databases, automatically packaging retrieved PDFs into ZIP.",
        date_sub: "Beijing Time (UTC+8) · Multi-Channel Auto Queue",
        ov_matrix_lbl: "Source Matrix",
        ov_matrix_unit: "OA Sources",
        ov_queue_lbl: "Queue Status",
        ov_progress_lbl: "Batch Progress",
        ov_engine_lbl: "Parallel Engine",
        ov_engine_unit: "Channels",
        p1_title: "1. Input or Upload DOIs",
        p1_sub: "Drag & drop a text file, or paste DOI list line by line",
        btn_clear_input: "🧹 Clear Input",
        drop_title: "Click to select DOI file or drag & drop here",
        drop_hint: "Supports .txt, .csv, .bib. Automatically extracts 10.xxxx/... format",
        lbl_doi_text: "Or paste DOI list directly (one per line):",
        lbl_concurrency: "Concurrent Channels (Hardware-tuned for 2C/12G VPS):",
        opt_conc_16: "🚀 16 Channels (Recommended Default · Balanced 2C/12G)",
        opt_conc_24: "⚡ 24 Channels (High Concurrency for 50+ DOIs)",
        opt_conc_12: "🟢 12 Channels (Balanced Rate-Limiting)",
        opt_conc_8: "🛡️ 8 Channels (Conservative Mode)",
        lbl_naming: "PDF Filename Scheme inside ZIP:",
        opt_name_title: "📑 Year_Author_Title (DOI).pdf (With paper title)",
        opt_name_doi: "🔢 Raw DOI only (e.g. 10.1038_nature12373.pdf)",
        btn_submit: "Start High-Concurrency Download & Archive",
        p2_title: "2. Live Progress & ZIP Archive",
        p2_sub: "Concurrent multi-source search with individual download & ZIP archive",
        btn_export_failed: "📋 Export Failed DOIs",
        btn_stop_pack: "⏹️ Stop & Archive Now",
        btn_reset_all: "🗑️ Reset Workspace",
        lbl_task_id: "Task ID:",
        lbl_task_conc: "Channels:",
        lbl_task_scope: "Search Scope:",
        btn_zip_reset_hint: "🧹 Finished downloading? Click here to clear task and reset workspace",
        th_idx: "#",
        th_doi: "DOI / Auto-Resolved Paper Title",
        th_source: "Matched Source",
        th_status: "Status / Download",
        tbl_empty: "Live retrieval status and download links will be displayed here once DOIs are submitted.",
        wb_title: "📋 Unresolved / Failed DOIs Export Whiteboard",
        opt_wb_pure: "Failed DOIs only (one per line)",
        opt_wb_all: "All unfetched DOIs (failed + incomplete)",
        opt_wb_title: "Failed DOIs + Year & Title comment",
        btn_wb_copy: "📑 Copy All",
        btn_wb_download: "💾 Download .txt",
        btn_wb_refill: "🔄 Refill to Input",
        wb_hint: "💡 Tip: DOIs can be selected directly or saved with \"📑 Copy All\" / \"💾 Download .txt\".",
        hist_title: "🕒 Task Queue & Recent Batches (Click to switch)",
        btn_hist_refresh: "Refresh",
        btn_hist_clear: "🗑️ Clear All",
        footer_left: "Batch DOI Retrieval & Archiving Engine · Open-Source Edition (v6.0)",
        footer_right: "Powered by Oracle Cloud Ampere ARM64 (2 OCPU / 12GB RAM / 2Gbps)",
        waiting_input: "Waiting for file upload or DOI list...",
        queue_idle_title: "🟢 System Idle & Ready",
        queue_idle_desc: "Upload or paste DOI list (one per line) to launch concurrent multi-channel retrieval across open repositories (Sci-Hub 4 mirrors, Unpaywall, OpenAlex, SemanticScholar, EuropePMC) with auto-ZIP packaging.",
        queue_busy_other_title: "⏳ Another user's task in progress",
        queue_busy_mine_title: "🚀 Your task is running at high speed",
        state_idle: "Idle",
        state_running: "Running",
        state_queued: "Queued",
        note_queue_idle: "Zero backlog · Immediate execution",
        status_pending: "Pending",
        status_fetching: "⚡ Fetching...",
        status_download: "✓ Download",
        status_failed: "Not Found",
        status_completed: "Completed",
        status_stopped: "Stopped",
        status_interrupted: "Interrupted",
        btn_hist_dl: "📦 Download ZIP",
        btn_hist_del: "✕ Delete",
        no_history: "No history batches recorded"
      }
    };

    let sciLang = localStorage.getItem("scihub_lang") || (navigator.language && !navigator.language.startsWith("zh") ? "en" : "zh");

    function applySciLang() {
      const dict = SCI_I18N[sciLang] || SCI_I18N.zh;
      document.documentElement.lang = sciLang === "zh" ? "zh-CN" : "en";
      document.title = dict.page_title;
      
      const btn = document.getElementById("sci-lang-btn");
      if (btn) btn.textContent = sciLang === "zh" ? "🌐 EN" : "🌐 中文";

      document.querySelectorAll("[data-i18n]").forEach(el => {
        const key = el.dataset.i18n;
        if (dict[key]) el.textContent = dict[key];
      });

      const ta = document.getElementById("failed-whiteboard-text");
      if (ta && (!ta.value || ta.value === "暂无下载失败的 DOI..." || ta.value === "No failed DOIs yet...")) {
        ta.placeholder = sciLang === "zh" ? "暂无下载失败的 DOI..." : "No failed DOIs yet...";
      }

      const stepEl = document.getElementById("step-msg");
      if (stepEl && (stepEl.textContent === "等待上传文件或粘贴 DOI列表..." || stepEl.textContent === "Waiting for file upload or DOI list..." || stepEl.textContent.startsWith("等待上传"))) {
        stepEl.textContent = dict.waiting_input;
      }
    }

    function toggleSciLang() {
      sciLang = (sciLang === "zh") ? "en" : "zh";
      localStorage.setItem("scihub_lang", sciLang);
      applySciLang();
      if (window._latestOverview) renderOverview(window._latestOverview);
      if (window._latestTaskObj) renderTask(window._latestTaskObj, window._latestQueueInfo);
      loadHistory();
    }

    function updateClock() {
      const now = new Date();
      const fmt = new Intl.DateTimeFormat(sciLang === "zh" ? "zh-CN" : "en-US", {
        timeZone: "Asia/Shanghai",
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
      }).format(now);
      document.getElementById("today-time").textContent = fmt;
    }
    updateClock();
    setInterval(updateClock, 1000);

    let currentTaskId = localStorage.getItem("last_scihub_task_id") || "";

    const dropZone = document.getElementById("drop-zone");
    dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("dragover"); });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", e => {
      e.preventDefault();
      dropZone.classList.remove("dragover");
      if (e.dataTransfer.files.length) {
        document.getElementById("doi-file").files = e.dataTransfer.files;
        onFileSelected(document.getElementById("doi-file"));
      }
    });

    function onFileSelected(input) {
      const f = input.files[0];
      const box = document.getElementById("selected-file-box");
      const nameEl = document.getElementById("selected-filename");
      const prefix = sciLang === "zh" ? "✓ 已选择: " : "✓ Selected: ";
      if (f) {
        box.hidden = false;
        nameEl.textContent = `${prefix}${f.name} (${(f.size / 1024).toFixed(1)} KB)`;
      } else {
        box.hidden = true;
      }
    }

    function renderOverview(ov) {
      if (!ov) return;
      window._latestOverview = ov;
      const dict = SCI_I18N[sciLang] || SCI_I18N.zh;
      const banner = document.getElementById("global-queue-banner");
      const bannerText = document.getElementById("global-queue-text");
      const qState = document.getElementById("ov-queue-state");
      const qNote = document.getElementById("ov-queue-note");
      const pillConcTxt = document.getElementById("pill-concurrency-text");

      if (ov.system_busy && ov.active_task) {
        const act = ov.active_task;
        const isMine = (act.task_id === currentTaskId);
        qState.textContent = ov.queued_count > 0
          ? (sciLang === "zh" ? `排队 ${ov.queued_count} 人` : `Queued (${ov.queued_count})`)
          : dict.state_running;
        qNote.textContent = sciLang === "zh"
          ? `正在执行 #${act.task_id} (剩余 ${act.remaining} 篇)`
          : `Executing #${act.task_id} (${act.remaining} remaining)`;
        if (pillConcTxt) {
          pillConcTxt.textContent = sciLang === "zh"
            ? `⚡ 高并发引擎: ${act.concurrency || 16}路全速运行中`
            : `⚡ Concurrency: ${act.concurrency || 16} channels active`;
        }

        if (!isMine) {
          banner.classList.remove("idle");
          bannerText.innerHTML = sciLang === "zh"
            ? `<strong>⏳ 其他用户正在使用系统</strong> — 当前正在处理任务 <code>#${act.task_id}</code>（已完成 ${act.done}/${act.total} 篇，还剩 <strong>${act.remaining}</strong> 篇）${ov.queued_count > 0 ? `，另有 <strong>${ov.queued_count}</strong> 个任务在排队` : ""}。您可以照常提交，系统会自动为您排号并在前序任务结束后立即开始！`
            : `<strong>⏳ Another task in progress</strong> — Processing task <code>#${act.task_id}</code> (${act.done}/${act.total} completed, <strong>${act.remaining}</strong> remaining)${ov.queued_count > 0 ? `, with <strong>${ov.queued_count}</strong> tasks queued` : ""}. Feel free to submit; your job will automatically queue and start right after!`;
        } else {
          banner.classList.add("idle");
          bannerText.innerHTML = sciLang === "zh"
            ? `<strong>🚀 您的任务 (#${act.task_id}) 正在以 ${act.concurrency || 16} 路并发高速执行中</strong> — 已完成 ${act.done}/${act.total} 篇，完成后将自动生成 ZIP 压缩包。`
            : `<strong>🚀 Your task (#${act.task_id}) is running at ${act.concurrency || 16} concurrent channels</strong> — ${act.done}/${act.total} completed. ZIP archive will be generated upon completion.`;
        }
      } else {
        qState.textContent = dict.state_idle;
        qNote.textContent = dict.note_queue_idle;
        const selVal = document.getElementById("concurrency-select")?.value || 16;
        if (pillConcTxt) {
          pillConcTxt.textContent = sciLang === "zh"
            ? `⚡ 高并发引擎: ${selVal}路就绪 (2C/12G)`
            : `⚡ Concurrency: ${selVal} Ready (2C/12G)`;
        }
        banner.classList.add("idle");
        bannerText.innerHTML = `<strong>${dict.queue_idle_title}</strong> — ${dict.queue_idle_desc}`;
      }
    }

    async function submitBatch() {
      const fileInput = document.getElementById("doi-file");
      const textInput = document.getElementById("doi-text").value;
      if (!fileInput.files.length && !textInput.trim()) {
        alert(sciLang === "zh" ? "请先上传 DOI 文件，或在输入框中粘贴 DOI 列表！" : "Please upload a DOI file or paste DOI list first!");
        return;
      }
      const btn = document.getElementById("submit-btn");
      btn.disabled = true;

      const fd = new FormData();
      if (fileInput.files.length) fd.append("doi_file", fileInput.files[0]);
      if (textInput.trim()) fd.append("doi_text", textInput);
      fd.append("concurrency", document.getElementById("concurrency-select").value);
      fd.append("naming_mode", document.getElementById("naming-mode").value);

      try {
        const r = await fetch("/api/submit", { method: "POST", body: fd });
        const d = await r.json();
        if (!d.ok) {
          alert(d.error || (sciLang === "zh" ? "提交失败" : "Submission failed"));
        } else {
          currentTaskId = d.task_id;
          localStorage.setItem("last_scihub_task_id", currentTaskId);
          await pollAll();
          await loadHistory();
        }
      } catch (e) {
        alert((sciLang === "zh" ? "网络异常: " : "Network error: ") + e);
      } finally {
        btn.disabled = false;
      }
    }

    function renderTask(task, qInfo) {
      window._latestTaskObj = task;
      window._latestQueueInfo = qInfo;
      const dict = SCI_I18N[sciLang] || SCI_I18N.zh;

      document.getElementById("st-id").textContent = task.task_id;
      const done = task.success_count + task.failed_count;
      const conc = task.concurrency || 16;
      document.getElementById("ov-cur-done").textContent = done;
      document.getElementById("ov-cur-total").textContent = sciLang === "zh" ? `/ ${task.total} 篇` : `/ ${task.total} DOIs`;
      document.getElementById("ov-cur-note").textContent = sciLang === "zh"
        ? `成功 ${task.success_count} 篇 · 未命中 ${task.failed_count} 篇`
        : `Success: ${task.success_count} · Missed: ${task.failed_count}`;
      document.getElementById("ov-concurrency").textContent = conc;
      document.getElementById("st-concurrency").textContent = task.status === "running"
        ? (sciLang === "zh" ? `${conc} 路并发检索中` : `${conc} channels fetching`)
        : (sciLang === "zh" ? `${conc} 路并发通道` : `${conc} channels ready`);

      const pct = task.total > 0 ? Math.round((done / task.total) * 100) : 0;
      document.getElementById("pct-text").textContent = pct + "%";
      document.getElementById("progress-fill").style.width = pct + "%";
      document.getElementById("step-msg").textContent = task.step_msg || dict.waiting_input;

      const qAlert = document.getElementById("my-queue-alert");
      if (task.status === "queued" && qInfo && qInfo.queue_position > 0) {
        const estMin = Math.max(1, Math.ceil((qInfo.est_wait_seconds || 15) / 60));
        qAlert.hidden = false;
        qAlert.innerHTML = sciLang === "zh" ? `
          <div><strong>⏳ 当前有其他用户正在使用系统，您的文件已进入自动排队队列！</strong></div>
          <div style="margin-top:4px;">
            • 您的排队顺位：<strong>第 #${qInfo.queue_position} 位</strong>（前方还有 <strong>${qInfo.ahead_tasks}</strong> 个任务、共 <strong>${qInfo.ahead_dois}</strong> 篇文献正在处理）<br>
            • 预计等待时间：约 <strong>${estMin} 分钟</strong>。前序用户处理完毕后系统将自动为您抓取并生成压缩包！
          </div>
        ` : `
          <div><strong>⏳ Another task is in progress. Your job has entered the automatic queue!</strong></div>
          <div style="margin-top:4px;">
            • Your Queue Position: <strong>#${qInfo.queue_position}</strong> (Ahead: <strong>${qInfo.ahead_tasks}</strong> tasks, <strong>${qInfo.ahead_dois}</strong> DOIs processing)<br>
            • Estimated Wait Time: ~<strong>${estMin} min</strong>. Once previous batches complete, retrieval will begin automatically!
          </div>
        `;
      } else {
        qAlert.hidden = true;
      }

      document.getElementById("stop-btn").hidden = !(task.status === "running" || task.status === "queued");
      document.getElementById("reset-btn").hidden = false;

      refreshFailedWhiteboardText(false);

      const zipBox = document.getElementById("zip-download-box");
      const zipLink = document.getElementById("zip-download-link");
      const zipTxt = document.getElementById("zip-download-text");
      if (task.zip_ready && (task.success_count > 0 || task.status === "completed")) {
        zipBox.hidden = false;
        zipLink.href = "/api/download/" + task.task_id;
        if (sciLang === "zh") {
          zipTxt.textContent = task.status === "completed"
            ? `📦 全部处理完成！点击下载文献压缩包 ZIP (含 ${task.success_count} 篇 PDF 与汇总清单)`
            : `📦 实时下载目前已抓到的文献压缩包 ZIP (已命中 ${task.success_count}/${task.total} 篇)`;
        } else {
          zipTxt.textContent = task.status === "completed"
            ? `📦 Finished! Download Literature Archive ZIP (${task.success_count} PDFs & Summary)`
            : `📦 Download Current ZIP Archive (${task.success_count}/${task.total} PDFs Retrieved)`;
        }
      } else {
        zipBox.hidden = true;
      }

      const tbody = document.getElementById("items-tbody");
      tbody.innerHTML = task.items.map((it, idx) => {
        let pill = `<span class="pill-status pill-pending">${dict.status_pending}</span>`;
        if (it.status === "fetching" || it.status === "fetching_p1") {
          pill = `<span class="pill-status pill-fetching">${dict.status_fetching}</span>`;
        } else if (it.status === "success") {
          const dlUrl = `/api/download/${task.task_id}/file/${encodeURIComponent(it.filename)}`;
          pill = `<a href="${dlUrl}" target="_blank" class="pill-status pill-success">${dict.status_download} (${it.size_kb}K)</a>`;
        } else if (it.status === "failed") {
          pill = `<span class="pill-status pill-failed">${dict.status_failed}</span>`;
        }
        const titleHtml = it.title
          ? `<div style="font-size:11.5px; color:var(--muted); margin-top:3px; white-space:normal;">${it.year ? '[' + it.year + '] ' : ''}${it.title}</div>`
          : "";
        return `<tr>
          <td style="color:var(--muted);">${idx + 1}</td>
          <td>
            <div style="font-weight:600; font-family:'SF Mono','JetBrains Mono',monospace;">${it.doi}</div>
            ${titleHtml}
          </td>
          <td style="color:var(--secondary); font-size:12px;">${it.source || "—"}</td>
          <td>${pill}</td>
        </tr>`;
      }).join("");
    }

    let _lastWbSignature = "";

    function refreshFailedWhiteboardText(forceUpdate) {
      const task = window._latestTaskObj;
      const wbBox = document.getElementById("failed-whiteboard-box");
      const expBtn = document.getElementById("export-failed-btn");
      if (!task || !task.items) {
        wbBox.hidden = true;
        expBtn.hidden = true;
        return;
      }

      const fmt = document.getElementById("wb-format-mode").value;
      const failedOnly = task.items.filter(it => it.status === "failed");
      const allUnfetched = task.items.filter(it => it.status !== "success");
      const targetItems = (fmt === "all_unfetched") ? allUnfetched : failedOnly;

      const hasFailedOrStopped = failedOnly.length > 0 || ((task.status === "cancelled" || task.status === "interrupted") && allUnfetched.length > 0);
      wbBox.hidden = !hasFailedOrStopped;
      expBtn.hidden = !hasFailedOrStopped;
      document.getElementById("export-failed-btn-cnt").textContent = targetItems.length;
      document.getElementById("wb-failed-count").textContent = sciLang === "zh" ? `${targetItems.length} 篇` : `${targetItems.length} DOIs`;

      const lines = targetItems.map(it => {
        if (fmt === "with_title" && it.title) {
          return `${it.doi}  # ${it.year ? '[' + it.year + '] ' : ''}${it.title}`;
        }
        return it.doi;
      });
      const textContent = lines.join("\n");
      const sig = `${task.task_id}|${fmt}|${lines.length}|${textContent}`;
      const ta = document.getElementById("failed-whiteboard-text");
      if (forceUpdate || (sig !== _lastWbSignature && document.activeElement !== ta)) {
        ta.value = textContent;
        _lastWbSignature = sig;
      }
    }

    function scrollToFailedWhiteboard() {
      const wbBox = document.getElementById("failed-whiteboard-box");
      wbBox.hidden = false;
      refreshFailedWhiteboardText(true);
      wbBox.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => {
        const ta = document.getElementById("failed-whiteboard-text");
        ta.focus();
        ta.select();
      }, 300);
    }

    async function copyFailedWhiteboard() {
      const ta = document.getElementById("failed-whiteboard-text");
      const val = ta.value.trim();
      if (!val) {
        alert(sciLang === "zh" ? "当前白板内没有需要复制的失败 DOI！" : "No failed DOIs in the whiteboard to copy!");
        return;
      }
      try {
        await navigator.clipboard.writeText(val);
      } catch (e) {
        ta.focus();
        ta.select();
        document.execCommand("copy");
      }
      const toast = document.getElementById("wb-copy-toast");
      const lineCnt = val.split(/\r?\n/).filter(Boolean).length;
      toast.textContent = sciLang === "zh" ? `✓ 已复制 ${lineCnt} 条失败 DOI 到剪贴板！` : `✓ Copied ${lineCnt} DOIs to clipboard!`;
      toast.hidden = false;
      setTimeout(() => { toast.hidden = true; }, 3200);
    }

    function downloadFailedWhiteboardTxt() {
      const ta = document.getElementById("failed-whiteboard-text");
      const val = ta.value.trim();
      if (!val) {
        alert(sciLang === "zh" ? "当前白板内没有失败的 DOI 可供下载！" : "No failed DOIs available to download!");
        return;
      }
      const blob = new Blob([val + "\n"], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Failed_DOIs_${currentTaskId || "export"}.txt`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1500);
    }

    function refillFailedToLeftInput() {
      const ta = document.getElementById("failed-whiteboard-text");
      const val = ta.value.trim();
      if (!val) return;
      const pureDois = val.split(/\r?\n/).map(l => l.split("#")[0].trim()).filter(Boolean).join("\n");
      const leftTa = document.getElementById("doi-text");
      leftTa.value = pureDois;
      leftTa.scrollIntoView({ behavior: "smooth", block: "center" });
      leftTa.focus();
    }

    function clearInputsOnly() {
      const fInput = document.getElementById("doi-file");
      if (fInput) fInput.value = "";
      document.getElementById("selected-file-box").hidden = true;
      document.getElementById("selected-filename").textContent = "";
      document.getElementById("doi-text").value = "";
    }

    function resetWorkspaceUI() {
      currentTaskId = "";
      window._latestTaskObj = null;
      _lastWbSignature = "";
      localStorage.removeItem("last_scihub_task_id");
      clearInputsOnly();

      const dict = SCI_I18N[sciLang] || SCI_I18N.zh;
      const selConc = document.getElementById("concurrency-select")?.value || "16";
      document.getElementById("st-id").textContent = "-";
      document.getElementById("ov-cur-done").textContent = "0";
      document.getElementById("ov-cur-total").textContent = sciLang === "zh" ? "/ 0 篇" : "/ 0 DOIs";
      document.getElementById("ov-cur-note").textContent = sciLang === "zh" ? "成功 0 篇 · 未命中 0 篇" : "Success: 0 · Missed: 0";
      document.getElementById("ov-concurrency").textContent = selConc;
      document.getElementById("st-concurrency").textContent = sciLang === "zh" ? `${selConc} 路并发就绪` : `${selConc} channels ready`;

      document.getElementById("pct-text").textContent = "0%";
      document.getElementById("progress-fill").style.width = "0%";
      document.getElementById("step-msg").textContent = dict.waiting_input;

      document.getElementById("my-queue-alert").hidden = true;
      document.getElementById("stop-btn").hidden = true;
      document.getElementById("reset-btn").hidden = true;
      document.getElementById("export-failed-btn").hidden = true;
      document.getElementById("zip-download-box").hidden = true;
      document.getElementById("failed-whiteboard-box").hidden = true;
      document.getElementById("failed-whiteboard-text").value = "";

      document.getElementById("items-tbody").innerHTML = `<tr><td colspan="4" style="text-align:center; padding:42px 16px; color:var(--muted);">${dict.tbl_empty}</td></tr>`;
    }

    async function clearAndResetCurrentTask() {
      const tid = currentTaskId;
      resetWorkspaceUI();
      if (tid) {
        try {
          await fetch(`/api/task/${tid}/clear`, { method: "POST" });
        } catch (e) {}
      }
      await pollAll();
      await loadHistory();
    }

    async function deleteSingleTask(tid, ev) {
      if (ev) ev.stopPropagation();
      if (tid === currentTaskId) {
        resetWorkspaceUI();
      }
      try {
        await fetch(`/api/task/${tid}/clear`, { method: "POST" });
      } catch (e) {}
      await pollAll();
      await loadHistory();
    }

    async function clearAllHistoryTasks() {
      resetWorkspaceUI();
      try {
        await fetch("/api/tasks/clear_all", { method: "POST" });
      } catch (e) {}
      await pollAll();
      await loadHistory();
    }

    async function pollAll() {
      try {
        if (currentTaskId) {
          const r = await fetch("/api/task/" + currentTaskId);
          if (r.ok) {
            const d = await r.json();
            renderOverview(d.overview);
            renderTask(d.task, d.queue_info);
            return;
          } else if (r.status === 404) {
            resetWorkspaceUI();
          }
        }
        const r2 = await fetch("/api/overview");
        if (r2.ok) {
          const d2 = await r2.json();
          renderOverview(d2);
        }
      } catch (e) {}
    }

    async function loadHistory() {
      try {
        const r = await fetch("/api/tasks");
        const d = await r.json();
        if (d.overview) renderOverview(d.overview);
        const box = document.getElementById("history-list");
        const dict = SCI_I18N[sciLang] || SCI_I18N.zh;
        if (!d.tasks || !d.tasks.length) {
          box.innerHTML = `<div style="font-size:12.5px; color:var(--muted); padding:8px 2px;">${dict.no_history}</div>`;
          return;
        }
        const stMap = {
          completed: `<span class="pill-status pill-success">${dict.status_completed}</span>`,
          running: `<span class="pill-status pill-fetching">${dict.status_fetching}</span>`,
          queued: `<span class="pill-status pill-pending">${dict.status_pending}</span>`,
          cancelled: `<span class="pill-status pill-pending">${dict.status_stopped}</span>`,
          interrupted: `<span class="pill-status pill-failed">${dict.status_interrupted}</span>`
        };
        box.innerHTML = d.tasks.map(t => {
          const qBadge = (t.status === "queued" && t.queue_info?.queue_position > 0)
            ? `<span style="color:var(--orange); font-size:11.5px; font-weight:600;">(${sciLang === "zh" ? "排队第 #" + t.queue_info.queue_position + " 位" : "Queue #" + t.queue_info.queue_position})</span>`
            : "";
          const subText = sciLang === "zh"
            ? `提交于 ${t.created_at} · ${t.concurrency || 16}路并发 · 成功 ${t.success_count}/${t.total} 篇`
            : `Submitted ${t.created_at} · ${t.concurrency || 16} channels · Succeeded ${t.success_count}/${t.total}`;
          return `<div class="history-row" onclick="selectTask('${t.task_id}')">
            <div>
              <div style="font-weight:600; font-size:13px;">${t.title} <span style="color:var(--muted); font-weight:400;">(#${t.task_id})</span> ${qBadge}</div>
              <div style="font-size:11.5px; color:var(--muted); margin-top:2px;">${subText}</div>
            </div>
            <div style="display:flex; align-items:center; gap:8px;">
              ${stMap[t.status] || t.status}
              ${t.zip_ready ? `<a href="/api/download/${t.task_id}" onclick="event.stopPropagation()" class="btn-soft" style="color:var(--green); border-color:#cbead7;">${dict.btn_hist_dl}</a>` : ""}
              <button type="button" class="btn-soft" onclick="deleteSingleTask('${t.task_id}', event)" style="padding:5px 9px; font-size:11.5px; color:var(--red); border-color:#f9d8dc;">${dict.btn_hist_del}</button>
            </div>
          </div>`;
        }).join("");
      } catch (e) {}
    }

    function selectTask(tid) {
      currentTaskId = tid;
      localStorage.setItem("last_scihub_task_id", tid);
      pollAll();
    }

    async function cancelCurrentTask() {
      if (!currentTaskId) return;
      const msg = sciLang === "zh"
        ? "确定要提前停止当前任务并打包已下载的文献吗？"
        : "Are you sure you want to stop the task and archive downloaded papers now?";
      if (!confirm(msg)) return;
      await fetch(`/api/task/${currentTaskId}/cancel`, { method: "POST" });
      await pollAll();
      await loadHistory();
    }

    // 初始化界面与语言
    applySciLang();
    pollAll();
    loadHistory();
    setInterval(pollAll, 1500);
    setInterval(loadHistory, 6000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=18890, log_level="info")

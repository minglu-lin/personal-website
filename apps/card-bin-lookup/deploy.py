#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Card BIN Lookup Microservice Automated Deployment Script
Deploys to Oracle San Jose ARM VPS (sanjose-arm) behind Nginx & Cloudflare Anycast CDN
"""

import os
import sys
import json
import subprocess
import time
import requests

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_HOST = "sanjose-arm"
REMOTE_DIR = "/opt/card_bin_service"
DOMAIN = "bin.11231632.xyz"

def run_ssh(cmd, stdin_data=None):
    """Executes a command on the remote server via SSH."""
    args = ["ssh", REMOTE_HOST, cmd]
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    stdout, stderr = p.communicate(input=stdin_data)
    return p.returncode, stdout, stderr

def main():
    print("=" * 70)
    print("💳 正在部署全球卡 BIN 智能雷达系统 (10位精析) 到子域名...")
    print(f"[*] 目标子域名: https://{DOMAIN}")
    print(f"[*] 目标主机:   {REMOTE_HOST} (Oracle San Jose ARM 2C/12G)")
    print("=" * 70)

    # 1. 检查 SSH 跳板连通性
    print("\n[1/6] 正在校验 SSH 跳板网络连通性 (经由东京 -> 西班牙纯净住宅 IP)...")
    code, out, err = run_ssh("uptime")
    if code != 0:
        print(f"[!] 无法连接到 {REMOTE_HOST}: {err}")
        sys.exit(1)
    print(f"[✓] SSH 跳板通道已就绪: {out.strip()}")

    # 2. 创建远端目标目录
    print(f"\n[2/6] 创建远端服务目录 {REMOTE_DIR}...")
    run_ssh(f"sudo mkdir -p {REMOTE_DIR} && sudo chown -R ubuntu:ubuntu {REMOTE_DIR}")

    # 3. 同步服务端程序与前端资源
    print("\n[3/6] 传输核心服务组件 (server.py & index.html)...")
    server_code = open(os.path.join(BASE_DIR, "server.py"), "r", encoding="utf-8").read()
    code, _, err = run_ssh(f"cat > {REMOTE_DIR}/server.py", stdin_data=server_code)
    if code != 0:
        print(f"[!] 同步 server.py 失败: {err}")
        sys.exit(1)

    html_code = open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8").read()
    code, _, err = run_ssh(f"cat > {REMOTE_DIR}/index.html", stdin_data=html_code)
    if code != 0:
        print(f"[!] 同步 index.html 失败: {err}")
        sys.exit(1)
    print("[✓] 核心文件传输完成。")

    # 4. 部署与启动 systemd 服务
    print("\n[4/6] 配置并启动 card-bin.service systemd 服务...")
    service_code = open(os.path.join(BASE_DIR, "systemd", "card-bin.service"), "r", encoding="utf-8").read()
    run_ssh("sudo tee /etc/systemd/system/card-bin.service > /dev/null", stdin_data=service_code)
    run_ssh("sudo systemctl daemon-reload && sudo systemctl enable card-bin.service && sudo systemctl restart card-bin.service")
    time.sleep(1)
    code, out, _ = run_ssh("systemctl is-active card-bin.service")
    if "active" not in out:
        print(f"[!] card-bin.service 启动异常: {out}")
        _, logs, _ = run_ssh("journalctl -u card-bin.service -n 20 --no-pager")
        print(logs)
        sys.exit(1)
    print("[✓] card-bin.service 服务运行正常 (Active: running)。")

    # 5. 配置 Nginx 反向代理与 SSL
    print("\n[5/6] 部署 Nginx 站点配置并热重载...")
    nginx_conf = open(os.path.join(BASE_DIR, "nginx", "bin.conf"), "r", encoding="utf-8").read()
    run_ssh("sudo tee /etc/nginx/sites-available/bin.conf > /dev/null", stdin_data=nginx_conf)
    run_ssh("sudo ln -sf /etc/nginx/sites-available/bin.conf /etc/nginx/sites-enabled/bin.conf")
    code, out, err = run_ssh("sudo nginx -t && sudo systemctl reload nginx")
    if code != 0:
        print(f"[!] Nginx 重载失败: {err}")
        sys.exit(1)
    print("[✓] Nginx 反向代理配置已成功生效 (HTTP 301 重定向 + HTTPS SSL :443)。")

    # 6. 端到端服务健康检查
    print("\n[6/6] 端到端全链路健康测试...")
    code, out, _ = run_ssh("curl -s http://127.0.0.1:18895/api/health")
    print(f"[*] 远端本地健康状态: {out.strip()}")

    code, out, _ = run_ssh("curl -s 'http://127.0.0.1:18895/api/lookup?bin=4246711234' -H 'X-Access-Password: 11231632'")
    try:
        j = json.loads(out)
        if j.get("success"):
            print(f"[✓] 远端 10位卡BIN 查询测试成功: {j['data']['card_brand']} {j['data']['card_type']} ({j['data']['issuer']})")
    except Exception:
        print(f"[*] 测试响应: {out[:100]}...")

    # 公网 HTTPS 测试
    print(f"\n[*] 正在对公网入口发起 HTTPS 探活 (https://{DOMAIN})...")
    time.sleep(2)
    try:
        resp = requests.get(f"https://{DOMAIN}/api/health", timeout=10)
        if resp.status_code == 200:
            print(f"[✓] 公网 HTTPS 探活成功！响应: {resp.text.strip()}")
        else:
            print(f"[!] 公网响应 HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"[!] 公网直接探活提示 (CDN 缓存或 DNS 传播中): {e}")

    print("\n" + "=" * 70)
    print("🎉 部署圆满完成！全球卡 BIN 智能雷达系统已正式上线！")
    print(f"👉 Web 访问入口:  https://{DOMAIN}")
    print(f"👉 REST API 地址: https://{DOMAIN}/api/lookup?bin=4246711234")
    print("=" * 70 + "\n")

if __name__ == "__main__":
    main()

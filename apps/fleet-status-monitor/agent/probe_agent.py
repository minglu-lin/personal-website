#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Private Fleet Telemetry Agent with Monthly Traffic Statistics & On-Demand Polling
- Reports metrics to Master Probe Server (status-k9x2.11231632.xyz)
- Maintains persistent monthly traffic accounting (resets on 1st of each month)
- Supports on-demand wake up / sleep to minimize CPU and network overhead
"""

import os
import sys
import time
import json
import urllib.request

SERVER_URL = "https://status-k9x2.11231632.xyz/api/report"
TOKEN = "k9x2_secure_probe_token_9842865"
NODE_ID = os.getenv("PROBE_NODE_ID", "__NODE_ID__")
STATE_FILE = "/etc/probe_traffic_state.json"

last_work_time = 0
last_total_time = 0
last_rx_bytes = 0
last_tx_bytes = 0
last_check_ts = time.time()

def get_cpu_percent():
    global last_work_time, last_total_time
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()
        fields = [float(x) for x in line.strip().split()[1:]]
        work_time = sum(fields[:3])
        total_time = sum(fields)
        if last_total_time == 0:
            last_work_time = work_time
            last_total_time = total_time
            return 0.0
        delta_work = work_time - last_work_time
        delta_total = total_time - last_total_time
        last_work_time = work_time
        last_total_time = total_time
        if delta_total > 0:
            return round((delta_work / delta_total) * 100, 1)
    except Exception:
        pass
    return 0.0

def get_mem():
    total, free, buffers, cached = 0, 0, 0, 0
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split(":")
                k = parts[0].strip()
                v = int(parts[1].split()[0])
                if k == "MemTotal": total = v
                elif k == "MemFree": free = v
                elif k == "Buffers": buffers = v
                elif k == "Cached": cached = v
        used = total - free - buffers - cached
        percent = round((used / total) * 100, 1) if total > 0 else 0
        return percent, f"{used // 1024} MB", f"{total // 1024} MB"
    except Exception:
        return 0, "0 MB", "0 MB"

def get_net():
    global last_rx_bytes, last_tx_bytes, last_check_ts
    rx_total, tx_total = 0, 0
    try:
        with open("/proc/net/dev", "r") as f:
            for line in f:
                if ":" in line:
                    iface, data = line.split(":", 1)
                    iface = iface.strip()
                    if iface != "lo" and not iface.startswith("tun") and not iface.startswith("warp"):
                        cols = data.split()
                        rx_total += int(cols[0])
                        tx_total += int(cols[8])
    except Exception:
        pass

    now = time.time()
    interval = now - last_check_ts if (now - last_check_ts) > 0 else 1.0

    if last_rx_bytes == 0:
        rx_speed = 0
        tx_speed = 0
    else:
        rx_speed = (rx_total - last_rx_bytes) / interval
        tx_speed = (tx_total - last_tx_bytes) / interval

    last_rx_bytes = rx_total
    last_tx_bytes = tx_total
    last_check_ts = now

    # Monthly accounting (UTC Month YYYY-MM)
    current_month = time.strftime("%Y-%m", time.gmtime(now))
    base_rx = -rx_total
    base_tx = -tx_total

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
            if saved.get("month") == current_month:
                base_rx = saved.get("base_rx", -rx_total)
                base_tx = saved.get("base_tx", -tx_total)
                last_raw_rx = saved.get("last_raw_rx", rx_total)
                last_raw_tx = saved.get("last_raw_tx", tx_total)
                # If system rebooted mid-month, raw counters reset to 0
                if rx_total < last_raw_rx:
                    base_rx += last_raw_rx
                if tx_total < last_raw_tx:
                    base_tx += last_raw_tx
            else:
                # New month started: offset raw bytes so new month begins at 0
                base_rx = -rx_total
                base_tx = -tx_total
        except Exception:
            pass

    month_rx = max(0, rx_total + base_rx)
    month_tx = max(0, tx_total + base_tx)

    state = {
        "month": current_month,
        "base_rx": base_rx,
        "base_tx": base_tx,
        "last_raw_rx": rx_total,
        "last_raw_tx": tx_total,
        "month_rx": month_rx,
        "month_tx": month_tx
    }

    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

    def fmt_speed(b):
        if b < 1024: return f"{int(b)} B/s"
        elif b < 1024 * 1024: return f"{b/1024:.1f} KB/s"
        else: return f"{b/(1024*1024):.1f} MB/s"

    def fmt_gb(b):
        return f"{b / (1024**3):.2f} GB"

    return fmt_speed(rx_speed), fmt_speed(tx_speed), fmt_gb(month_rx), fmt_gb(month_tx)

def get_uptime_and_load():
    uptime_str = "N/A"
    load_str = "0.00"
    try:
        with open("/proc/uptime", "r") as f:
            sec = int(float(f.read().split()[0]))
            d = sec // 86400
            h = (sec % 86400) // 3600
            m = (sec % 3600) // 60
            if d > 0: uptime_str = f"{d}天 {h}时 {m}分"
            else: uptime_str = f"{h}时 {m}分"
    except Exception: pass
    try:
        with open("/proc/loadavg", "r") as f:
            load_str = f.read().split()[0]
    except Exception: pass
    return uptime_str, load_str

# PROBE_ON_DEMAND_V1
def reset_sampling():
    global last_work_time, last_total_time, last_rx_bytes, last_tx_bytes, last_check_ts
    last_work_time = last_total_time = last_rx_bytes = last_tx_bytes = 0
    last_check_ts = time.time()

def wait_for_viewer():
    url = SERVER_URL.rsplit('/', 1)[0] + '/control?wait=25'
    request = urllib.request.Request(
        url,
        headers={
            'X-Probe-Token': TOKEN,
            'Accept': 'application/json',
            'Cache-Control': 'no-store',
            'User-Agent': 'Minglu-Probe-Agent/1.0'
        }
    )
    with urllib.request.urlopen(request, timeout=33) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError('control')
    return result

def main():
    generation = None
    while True:
        try:
            control = wait_for_viewer()
            if control.get('active') is not True:
                generation = None
                reset_sampling()
                # Periodically record network bytes even during sleep to guard against unrecorded reboots
                get_net()
                continue
            current = control.get('generation')
            if not isinstance(current, str):
                raise ValueError('generation')
            if generation != current:
                reset_sampling()
                generation = current
            cpu = get_cpu_percent()
            mem_percent, mem_used, mem_total = get_mem()
            rx_speed, tx_speed, total_in, total_out = get_net()
            uptime, load = get_uptime_and_load()
            payload = {
                'id': NODE_ID,
                'generation': generation,
                'metrics': {
                    'cpu': cpu,
                    'mem_percent': mem_percent,
                    'mem_used': mem_used,
                    'mem_total': mem_total,
                    'rx_speed': rx_speed,
                    'tx_speed': tx_speed,
                    'total_in': total_in,
                    'total_out': total_out,
                    'uptime': uptime,
                    'load': load
                }
            }
            request = urllib.request.Request(
                SERVER_URL,
                data=json.dumps(payload).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'X-Probe-Token': TOKEN,
                    'User-Agent': 'Minglu-Probe-Agent/1.0'
                },
                method='POST'
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
            if result.get('status') == 'paused':
                generation = None
                reset_sampling()
                continue
            time.sleep(3)
        except Exception:
            generation = None
            reset_sampling()
            time.sleep(5)

if __name__ == "__main__":
    main()

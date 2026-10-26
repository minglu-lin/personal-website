# 🧰 MyIP Toolbox Integration Module

This module packages and manages the **MyIP** all-in-one IP intelligence & network risk diagnostic toolbox, ready for deployment on any VPS host.

## 🌟 Key Features

- **Multi-Source IP Geolocation**: Detects IPv4 / IPv6, ASN, ISP, country, city, and time zone.
- **Network Quality & Risk Audit**: Residential vs. Datacenter IP detection, 0–100 fraud score assessment.
- **Privacy Leak Audit**: WebRTC real IP leak detection, DNS hijacking & contamination check.
- **Speed & Latency**: Worldwide Globalping probes, MTR traceroute, ping tests.

---

## 🚀 Quick Deployment (Docker)

```bash
# 1. Enter module directory
cd apps/myip

# 2. Configure environment (Optional: add MaxMind credentials for offline GeoIP)
cp .env.example .env
nano .env

# 3. Start container
docker compose up -d

# 4. Verify status
curl http://127.0.0.1:18966
```

## 🌐 Reverse Proxy (Nginx)

Copy the included `nginx/ip.conf` to `/etc/nginx/sites-available/ip.conf` and reload Nginx:
```bash
sudo cp nginx/ip.conf /etc/nginx/conf.d/ip.conf
sudo nginx -t && sudo systemctl reload nginx
```

---

## 🔗 Source Code Reference

The upstream source code is tracked via Git submodule at `apps/myip/src` from [minglu-lin/MyIP](https://github.com/minglu-lin/MyIP).

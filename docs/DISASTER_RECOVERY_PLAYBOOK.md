# 🛡️ 灾难恢复与 VPS 快速迁移手册 (Disaster Recovery Playbook)

> **目标**：当发生 VPS 突发失联、IP 被墙、云服务商封号或实例销毁时，在 **5 分钟内** 将个人网站及全套部署服务（MyIP、卡 BIN、探针、邮箱界面等）完整恢复到新机器上。

---

## ⚡ 极速冷恢复流程（仅需 3 步）

### 第一步：准备一台全新 Linux VPS
无论是各大云厂商（AWS、Azure、Oracle、Linode、DigitalOcean、Contabo、搬瓦工等），操作系统推荐 Ubuntu 22.04/24.04 或 Debian 11/12。

### 第二步：执行一键冷恢复脚本
以 root 身份登录新服务器，执行以下单行命令：

```bash
curl -fsSL https://raw.githubusercontent.com/minglu-lin/personal-website/main/scripts/one-key-restore.sh | sudo bash -s -- lmlu.de
```

脚本将全自动完成：
1. 安装 Docker、Docker Compose、Nginx、Git 与 Python3 环境；
2. 拉取本 GitHub 仓库及所有子模块（含 MyIP）；
3. 启动 MyIP Docker 容器（端口 18966）；
4. 挂载 Nginx 反向代理矩阵（主页、`ip.`、`bin.`、`status.`、`mail.`）；
5. 配置并激活 Systemd 后台常驻服务；
6. 自动颁发/挂载临时证书并完成健康巡检。

### 第三步：切换 Cloudflare DNS 解析
登录 [Cloudflare 控制台](https://dash.cloudflare.com/)：
1. 找到目标域名（如 `lmlu.de` 或 `11231632.xyz`）；
2. 将以下 A 记录的 IP 批量修改为**新 VPS 的公网 IP**：
   - `@` (主站)
   - `www`
   - `ip` (MyIP 工具箱)
   - `bin` (全球卡 BIN 智能雷达)
   - `status-k9x2` (集群探针)
   - `mail` (域名邮箱前端)
3. 确保云朵图标维持开启（🟠 Proxied），Cloudflare Anycast CDN 将在 1 秒内全球生效并自动代理 HTTPS 权威证书！

---

## 📦 各服务端口与路径清单

| 服务名称 | 二级域名 | 本地监听端口 | 运行形态 | 目录位置 |
| :--- | :--- | :---: | :---: | :--- |
| **学术个人主页** | `lmlu.de` | Nginx 80/443 | 静态托管 | `sites/homepage` |
| **MyIP 工具箱** | `ip.lmlu.de` | `127.0.0.1:18966` | Docker 容器 | `apps/myip` |
| **卡 BIN 智能雷达** | `bin.lmlu.de` | `127.0.0.1:18895` | Python FastAPI | `apps/card-bin-lookup` |
| **探针遥测大盘** | `status-k9x2.lmlu.de` | `127.0.0.1:18889` | Python + WS | `apps/fleet-status-monitor` |
| **Cloud 域名邮箱** | `mail.lmlu.de` | Nginx 80/443 | 静态 + CF Worker | `apps/cloudflare-domain-mail` |
| **备用极客伪装页** | - | - | 静态备选 | `sites/camouflage` |

---

## 🔒 敏感信息与安全规范

1. 本仓库为 **Public** 开源规范仓库，严格遵循脱敏工程原则；
2. 任何生产私钥、密码和 API Token 仅存放于各自的 `.env` 本地私有文件中，不被 Git 追踪；
3. 新服务器部署后，如需启用 MyIP 离线 IP 地理数据库，只需在 `apps/myip/.env` 中填入免费申请的 `MAXMIND_LICENSE_KEY`，执行 `docker compose restart` 即可。

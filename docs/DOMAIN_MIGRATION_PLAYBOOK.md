# 🌐 域名变更与无缝切换实操指南 (Domain Migration Playbook)

> **场景**：当你需要为个人网站更换新顶级域名（例如从 `11231632.xyz` 全面平移至 `lmlu.de`，或未来购入更简短的个人品牌域名时），如何零遗漏、零宕机完成全栈配置切换。

---

## 🚀 一键全库自动化域名切换

仓库内置自动化脚本 `scripts/switch-domain.py`，支持一键遍历修改全站 HTML、Schema.org 元数据、OpenGraph 标签、Nginx 虚拟主机配置与 Docker 环境变量。

### 1. 模拟预览（Dry-Run）
在未执行实际写入前，首先验证将被替换的文件与统计处数：
```bash
python scripts/switch-domain.py --from 11231632.xyz --to lmlu.de --dry-run
```

### 2. 执行正式替换
```bash
python scripts/switch-domain.py --from 11231632.xyz --to lmlu.de
```

脚本将自动级联替换：
- **顶级域名**：所有 HTML 规范链接（Canonical URL）、JSON-LD 个人资料、页脚版权；
- **二级域名矩阵**：
  - `ip.11231632.xyz` ➡️ `ip.lmlu.de`
  - `bin.11231632.xyz` ➡️ `bin.lmlu.de`
  - `status-k9x2.11231632.xyz` ➡️ `status-k9x2.lmlu.de`
  - `mail.11231632.xyz` ➡️ `mail.lmlu.de`
- **反向代理配置**：`deploy/nginx/conf.d/*.conf` 中的所有 `server_name` 项；
- **邮件路由标识**：`*@11231632.xyz` ➡️ `*@lmlu.de`。

### 3. 服务器热重载
将更新提交推送到仓库后，在 VPS 端拉取并重载：
```bash
git pull
sudo cp deploy/nginx/conf.d/*.conf /etc/nginx/conf.d/
sudo nginx -t && sudo systemctl reload nginx
cd apps/myip && docker compose up -d
```

---

## ☁️ Cloudflare 域名托管配置检查表

在新域名的 Cloudflare 控制台添加对应 DNS 记录：

| 记录类型 | 主机名 | 指向内容 (Target) | 代理状态 (Proxy) | 说明 |
| :---: | :--- | :--- | :---: | :--- |
| **A** | `@` | 你的 VPS IP | 🟠 Proxied | 个人学术主页主站 |
| **CNAME** | `www` | 目标域名根 (如 `lmlu.de`) | 🟠 Proxied | WWW 别名解析 |
| **A** | `ip` | 你的 VPS IP | 🟠 Proxied | MyIP 工具箱 |
| **A** | `bin` | 你的 VPS IP | 🟠 Proxied | 全球卡 BIN 雷达 |
| **A** | `status-k9x2` | 你的 VPS IP | 🟠 Proxied | 私密集群探针大盘 |
| **A** | `mail` | 你的 VPS IP | 🟠 Proxied | 域名邮箱控制台 |

---

## 🔄 双域名平滑过渡方案（Dual-Domain Coexistence）

如果希望旧域名继续可用，同时启用新域名：
1. Nginx 配置文件中默认均支持**多域名并列**：
   ```nginx
   server_name lmlu.de 11231632.xyz;
   ```
2. Cloudflare 将两个域名的 DNS 均解析到同一台 VPS IP 即可实现双域名并行访问。

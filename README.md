# sbm — sing-box 节点、用户与订阅管理

一台订阅服务器集中管理多台 [sing-box](https://github.com/SagerNet/sing-box) 节点：增删用户、一键部署节点、生成多客户端订阅、按用户统计流量，带网页控制台和命令行。

协议使用 **AnyTLS**，订阅同时支持 sing-box、Hiddify、Clash Meta / Mihomo、Clash Verge Rev、Shadowrocket 等客户端。

## 功能

- **用户管理**：新增、停用、删除、重置；新增用户**不重启节点**（账号池预置备用账号），删除/停用默认在每天凌晨统一清理，也可立即踢下线
- **订阅**：同一个链接按客户端自动返回 sing-box JSON / Clash YAML / 通用链接；每个用户有专属「使用说明页」，含下载、一键导入按钮、二维码；控制台可查看每个用户的订阅配置细节（概览 + 实际下发的原文，密码默认打码）
- **代理组与分流**（参照 ACL4SSR 精简分组）：节点选择、自动选择、故障转移、按节点名自动生成的地区组，以及 AI 服务（默认美国节点）、YouTube、Netflix、Telegram、微软、苹果、广告拦截、全球直连、漏网之鱼；sing-box 与 Clash 两种格式分组和规则一致（规则数据来自 MetaCubeX/meta-rules-dat）
- **节点管理**：
  - 一键部署新服务器：自动添加 Cloudflare 解析、申请证书、安装 sing-box、写配置、放行防火墙
  - 接入已有 sing-box 节点
  - sing-box 版本查看与批量升级（失败自动回滚）
  - 节点状态：运行状态、CPU 负载、内存、系统版本
- **国内可达性检测与告警**：订阅服务器每 5 分钟从国内检测各节点（国内 DNS、TCP/TLS 握手、经节点真实代理一次，失败时由其他节点从境外对照），区分「疑似被封 / 节点故障 / DNS 污染 / 代理不通」；异常时通过企业微信、飞书、钉钉、Bark、Telegram 告警
- **告警与每周简报**：证书快到期、节点 sing-box 停止、账号同步失败、流量采集停止、控制台被暴力破解、磁盘快满、备份失败、定时任务失败（systemd OnFailure）；去重、定期提醒、恢复通知；每周一发送简报；没有渠道时也记录在控制台
- **自动异地备份**：每天备份配置、用户、证书与 Token、统计数据，本机保留 14 份，AES-256 加密后复制到境外节点；控制台可下载、查看恢复密钥
- **流量统计**：按用户、按天、按节点统计，控制台查看每日图表；客户端可显示本月已用流量
- **域名管理**：多个主域名，每个配独立的 Cloudflare Token，部署节点时选择
- **证书**：订阅服务器统一申请和续期，续期后自动推送到节点
- **网页控制台**：深浅色主题、手机可用；附完整管理手册（控制台右上角「使用手册」）

## 架构

```
用户设备 ──AnyTLS──▶ sing-box 节点（可多台）◀──SSH 密钥管理── 订阅服务器
   │                                                         │
   └────────── 订阅 / 使用说明页（HTTPS）◀───────────────────┘
                                         控制台 · sbm 命令行 · 每日维护 · 流量采集
```

- 用户流量**不经过**订阅服务器，订阅服务器只负责管理和下发配置
- 订阅服务器通过一把专用 SSH 密钥管理节点，节点侧限制只接受订阅服务器 IP 的登录
- Cloudflare Token 只保存在订阅服务器上（仅 root 可读），不会下发到节点

## 目录

| 路径 | 说明 | 安装位置 |
|---|---|---|
| `bin/sbm` | 命令行工具，全部核心逻辑（Python 3，仅标准库） | `/usr/local/bin/sbm` |
| `web/app.py` | 控制台后端（只监听 127.0.0.1:8090，由 Nginx 做 HTTPS 和登录认证） | `/opt/sbm-web/` |
| `web/index.html` | 控制台页面 | `/opt/sbm-web/` |
| `web/manual.html` | 管理手册（`{{...}}` 占位符由后端按配置填入） | `/opt/sbm-web/` |
| `web/guide.tpl.html` | 用户使用说明页模板 | `/usr/local/share/sbm/` |
| `nginx/sub.conf.tpl` | Nginx 站点模板（订阅、使用说明页、控制台） | `/etc/nginx/sites-available/sub.conf` |
| `systemd/` | 控制台服务；定时任务：备份（03:30）、维护（04:00）、流量采集 / 可达性检测 / 健康检查（各每 5 分钟）、周报（周一 09:00）；任务失败告警 | `/etc/systemd/system/` |
| `contrib/certbot-restart-sing-box.sh` | 给「接入」节点用的 certbot 续期钩子 | 节点的 `/etc/letsencrypt/renewal-hooks/deploy/` |
| `install.sh` | 订阅服务器安装 / 更新脚本 | — |

运行时数据（不在仓库里）：`/etc/sbm/`（配置、用户、账号池、流量与检测库、通知渠道、备份密钥）、`/var/backups/sbm/`（本机备份）、`/etc/letsencrypt/`（证书与 Token）、`/root/.ssh/sbm_node`（管理节点的私钥）。

## 部署

### 准备

- **订阅服务器**：Ubuntu 24.04（已验证）或其他较新的 Debian / Ubuntu，1 核 1 GB 足够，需要 root
- **域名**：DNS 托管在 Cloudflare。把订阅域名（如 `sub.example.com`）的 A 记录指向订阅服务器 IP，**关闭代理（灰色云朵）**
- **Cloudflare API Token**：My Profile → API Tokens → Create Token → *Edit zone DNS* 模板，Zone Resources 只选这个域名
- **安全组**：放行订阅端口（默认 TCP 8443）
- **节点**：任意能 SSH 的 Linux VPS（Debian、Ubuntu、CentOS 7+ 已测试），安全组放行节点端口（默认 TCP 443）

> 为什么默认 8443：订阅服务器如果在中国大陆，未备案域名不能使用 80/443。证书用 Cloudflare DNS 验证，不需要 80 端口。服务器在境外可以改用 `--port 443`。

### 安装订阅服务器

```bash
git clone https://github.com/yuuolo/sbm.git
cd sbm
sudo ./install.sh --domain sub.example.com
```

安装过程中会提示输入 Cloudflare Token（不回显）和管理员密码。完成后打开 `https://sub.example.com:8443/admin/`。

可选参数：`--port 8443`（订阅与控制台端口）、`--profile 名称`（客户端里显示的配置名，默认取主域名第一段）。

### 添加第一个节点

1. 控制台 → 节点 → **添加节点**，复制显示的授权命令，用 root 在新服务器上运行一次
2. 选择「一键部署新服务器」，填写节点名称、IP、域名前缀，点开始；约 1~2 分钟完成
3. 控制台 → **新增用户**，把弹窗第一项「使用说明页」发给对方

### 更新

```bash
cd sbm && git pull
sudo ./install.sh --upgrade
```

只替换程序文件并重启控制台服务，不改动配置、用户、证书和密码。

## 命令行

```bash
sudo sbm user list | add <名字> | show <名字> | config <名字> [--format singbox|clash|uri] | disable/enable <名字> | del <名字> [--now] | reset <名字>
sudo sbm node list | pubkey | deploy <名称> --host <IP> --sub <前缀> [--zone 域名] | attach | del | versions | upgrade [--all]
sudo sbm domain list | add <域名> | check | token | default | del
sudo sbm traffic [collect]
sudo sbm probe [run]      # 国内可达性
sudo sbm notify list | test
sudo sbm health           # 健康检查
sudo sbm report [send]    # 每周简报
sudo sbm backup [run|key] # 备份
sudo sbm pool          # 账号池与待清理账号
sudo sbm sync          # 立即执行每日维护
sudo sbm --help
```

## 设计要点

- **不重启加用户**：sing-box 只在启动时读取用户列表。节点上始终放着「启用用户 + 备用账号 + 待清理账号」，新增用户只是分配一个已存在的备用账号；配置内容不变时推送会判断为未变化，不重启。
- **按用户统计流量**：官方 sing-box 没有按用户计数。sbm 为每个账号生成专属 direct 出站（带唯一 `routing_mark`）和 `auth_user` 路由规则，节点上用 iptables（mangle 表 + CONNMARK）按标记计数，订阅服务器每 5 分钟汇总到 SQLite。
- **安全**：控制台只监听本机，由 Nginx 做 HTTPS 和 bcrypt 密码认证；写操作需要自定义请求头防 CSRF；控制台进程运行在 systemd 沙箱里；Token 文件 600 权限、接口不返回 Token。

## 注意

- 运行环境验证情况：订阅服务器 Ubuntu 24.04；节点 Ubuntu 22.04 / 24.04、CentOS 7；sing-box 1.14
- `install.sh` 汇总了现有环境的手工部署步骤，尚未在一台全新服务器上完整跑过；遇到问题欢迎提 Issue
- 请自行评估服务器所在地法规和云服务商条款的风险

## 许可

[MIT](LICENSE)

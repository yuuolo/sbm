#!/bin/bash
# sbm 订阅服务器安装脚本（Debian / Ubuntu，需要 root）
#
# 首次安装：
#   sudo ./install.sh --domain sub.example.com [--port 8443] [--profile 名称]
# 更新程序（已安装的服务器，只替换程序文件，不动数据、证书和密码）：
#   sudo ./install.sh --upgrade
#
# 安装前请准备：
#   1. 一个托管在 Cloudflare 的域名，并把订阅域名（如 sub.example.com）解析到本机 IP（不开代理/小黄云）
#   2. 该域名的 Cloudflare API Token（Edit zone DNS 模板，只授权这个域名），安装过程中会提示输入
#   3. 云服务商安全组放行订阅端口（默认 TCP 8443）
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="" PORT=8443 PROFILE="" UPGRADE=0

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --upgrade) UPGRADE=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "未知参数: $1" >&2; usage 1 ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "请用 root 运行（sudo ./install.sh ...）" >&2; exit 1; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [ "$UPGRADE" = 1 ]; then
    [ -f /etc/sbm/config.json ] || { echo "还没有安装过，请去掉 --upgrade 进行首次安装" >&2; exit 1; }
    DOMAIN=$(python3 -c 'import json; print(json.load(open("/etc/sbm/config.json"))["sub_domain"])')
    PORT=$(python3 -c 'import json; print(json.load(open("/etc/sbm/config.json"))["sub_port"])')
    PROFILE=$(python3 -c 'import json; print(json.load(open("/etc/sbm/config.json")).get("profile_name", "sub"))')
else
    [ -n "$DOMAIN" ] || { echo "缺少 --domain" >&2; usage 1; }
fi
[[ "$DOMAIN" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || { echo "订阅域名格式不对: $DOMAIN" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "端口格式不对: $PORT" >&2; exit 1; }
BASE_DOMAIN="${DOMAIN#*.}"
PROFILE="${PROFILE:-${BASE_DOMAIN%%.*}}"

step "安装依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx certbot python3-certbot-dns-cloudflare python3-qrcode jq apache2-utils curl >/dev/null

step "安装程序文件"
install -m 755 "$SRC/bin/sbm" /usr/local/bin/sbm
install -d -m 755 /opt/sbm-web /usr/local/share/sbm
install -m 644 "$SRC/web/app.py" "$SRC/web/index.html" "$SRC/web/manual.html" /opt/sbm-web/
install -m 644 "$SRC/web/guide.tpl.html" /usr/local/share/sbm/
install -m 644 "$SRC"/systemd/*.service "$SRC"/systemd/*.timer /etc/systemd/system/
sed -e "s/{{SUB_DOMAIN}}/$DOMAIN/g" -e "s/{{SUB_PORT}}/$PORT/g" -e "s/{{PROFILE_NAME}}/$PROFILE/g" \
    "$SRC/nginx/sub.conf.tpl" > /etc/nginx/sites-available/sub.conf
echo "已安装 sbm、管理后台、Nginx 站点配置和 systemd 服务"

if [ "$UPGRADE" = 0 ]; then
    step "初始化配置 /etc/sbm"
    install -d -m 700 /etc/sbm
    if [ ! -f /etc/sbm/config.json ]; then
        MANAGER_IP=$(curl -4 -s --max-time 10 https://api.ipify.org || true)
        [ -n "$MANAGER_IP" ] || read -rp "无法自动获取本机公网 IP，请输入: " MANAGER_IP
        jq -n --arg d "$DOMAIN" --argjson p "$PORT" --arg pr "$PROFILE" --arg b "$BASE_DOMAIN" --arg ip "$MANAGER_IP" \
            '{sub_domain: $d, sub_port: $p, profile_name: $pr, base_domain: $b, manager_ip: $ip,
              pool_size: 20, maintenance_time: "04:00", nodes: []}' > /etc/sbm/config.json
        chmod 600 /etc/sbm/config.json
        echo "已生成 /etc/sbm/config.json（本机公网 IP: $MANAGER_IP，用于限制节点只接受本机的 SSH 管理）"
    else
        echo "已存在，保留不动"
    fi

    step "管理节点用的 SSH 密钥"
    install -d -m 700 /root/.ssh
    if [ ! -f /root/.ssh/sbm_node ]; then
        ssh-keygen -q -t ed25519 -N '' -C "sbm@$DOMAIN" -f /root/.ssh/sbm_node
        echo "已生成 /root/.ssh/sbm_node"
    else
        echo "已存在，保留不动"
    fi

    step "Cloudflare API Token（$BASE_DOMAIN）"
    if ! grep -qE '^dns_cloudflare_api_token *= *[A-Za-z0-9_-]{20,}' /etc/letsencrypt/cloudflare.ini 2>/dev/null; then
        install -d -m 755 /etc/letsencrypt
        read -rsp "粘贴 Token（输入时不显示）: " T; echo
        ( umask 077; printf 'dns_cloudflare_api_token = %s\n' "$T" > /etc/letsencrypt/cloudflare.ini )
        unset T
        sbm domain check "$BASE_DOMAIN" || { echo "Token 检测失败，请确认 Token 权限后重新运行安装" >&2; exit 1; }
    else
        echo "已存在，保留不动"
    fi

    step "申请 $DOMAIN 的证书（Cloudflare DNS 验证，约 1 分钟）"
    if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
        certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
            --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
            --dns-cloudflare-propagation-seconds 30 --deploy-hook "systemctl reload nginx" -d "$DOMAIN"
    else
        echo "已存在，保留不动"
    fi
    # 续期后把证书推送到 sbm 部署的节点
    install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
    printf '#!/bin/sh\nexec /usr/local/bin/sbm cert-renewed $RENEWED_DOMAINS\n' > /etc/letsencrypt/renewal-hooks/deploy/sbm-push.sh
    chmod 755 /etc/letsencrypt/renewal-hooks/deploy/sbm-push.sh

    step "管理员账号"
    [ -f /etc/nginx/sbm-userinfo.map ] || install -m 644 /dev/null /etc/nginx/sbm-userinfo.map
    if [ ! -s /etc/nginx/sbm-admin.htpasswd ]; then
        read -rp "管理员用户名 [admin]: " ADMIN; ADMIN="${ADMIN:-admin}"
        htpasswd -B -c /etc/nginx/sbm-admin.htpasswd "$ADMIN"
        chown root:www-data /etc/nginx/sbm-admin.htpasswd; chmod 640 /etc/nginx/sbm-admin.htpasswd
    else
        echo "已存在，保留不动（改密码：htpasswd -B /etc/nginx/sbm-admin.htpasswd 用户名）"
    fi
fi

step "启用 Nginx 站点和服务"
[ -f /etc/nginx/sbm-userinfo.map ] || install -m 644 /dev/null /etc/nginx/sbm-userinfo.map
ln -sf /etc/nginx/sites-available/sub.conf /etc/nginx/sites-enabled/sub.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx || systemctl restart nginx
systemctl daemon-reload
install -d -m 700 /var/backups/sbm
systemctl enable --now sbm-web.service sbm-maintain.timer sbm-traffic.timer sbm-probe.timer \
    sbm-health.timer sbm-backup.timer sbm-report.timer >/dev/null 2>&1
systemctl restart sbm-web.service
systemctl is-active --quiet sbm-web || { journalctl -u sbm-web -n 20 --no-pager; exit 1; }
if [ "$UPGRADE" = 0 ]; then
    # 生成账号池（还没有节点时不会推送任何东西）
    sbm sync >/dev/null || echo "⚠ 首次 sbm sync 失败，可稍后手动运行 sbm sync" >&2
fi

step "完成"
cat <<EOF
控制台:   https://$DOMAIN:$PORT/admin/
下一步:   在控制台「节点 → 添加节点」部署第一个节点，然后「新增用户」。
          新节点需要先以 root 运行授权命令（控制台里会显示，也可以运行 sbm node pubkey 查看）。
命令行:   sbm --help
EOF

#!/bin/sh
# certbot 续期成功后的部署钩子：如果续期的证书正被 sing-box 使用，就校验配置并重启 sing-box，
# 让新证书生效（sing-box 只在启动时加载证书）。
# certbot 提供的环境变量：RENEWED_LINEAGE=/etc/letsencrypt/live/<域名>，RENEWED_DOMAINS=本次续期的域名
CONFIG=/etc/sing-box/config.json

if [ -z "$RENEWED_LINEAGE" ] || ! grep -qF "\"$RENEWED_LINEAGE/" "$CONFIG"; then
    exit 0
fi

if ! sing-box check -c "$CONFIG"; then
    logger -t certbot-sing-box "证书 $RENEWED_DOMAINS 已续期，但 sing-box 配置校验失败，未重启"
    exit 1
fi

systemctl restart sing-box
sleep 2
if systemctl is-active --quiet sing-box; then
    logger -t certbot-sing-box "证书 $RENEWED_DOMAINS 已续期，sing-box 已重启"
else
    logger -t certbot-sing-box "证书 $RENEWED_DOMAINS 已续期，但 sing-box 重启失败"
    exit 1
fi

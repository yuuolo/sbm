# sing-box 订阅服务（由 sbm 生成文件，Nginx 只负责分发）
# 显式参数 ?t=singbox|clash|uri 优先，否则按 User-Agent 识别客户端
map $arg_t $sub_fmt_arg {
    singbox singbox.json;
    clash   clash.yaml;
    uri     uri.txt;
    default "";
}

# 正则按书写顺序匹配：Hiddify 的 UA 里也带 ClashMeta 字样，必须放在最前面
map $http_user_agent $sub_fmt_ua {
    ~*hiddify                     singbox.json;
    ~*(sing-box|sfa/|sfi/|sfm/|sft/) singbox.json;
    ~*(clash|mihomo|stash|verge)  clash.yaml;
    default                       uri.txt;
}

map $sub_fmt_arg $sub_fmt {
    ""      $sub_fmt_ua;
    default $sub_fmt_arg;
}

# 本月已用流量，由 sbm traffic collect 每 5 分钟生成；客户端从 subscription-userinfo 头读取
map $token $sbm_userinfo {
    default "";
    include /etc/nginx/sbm-userinfo.map;
}

limit_req_zone $binary_remote_addr zone=sub:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=admin:10m rate=60r/m;

server {
    # Nginx 1.24 还不支持单独的 http2 指令，写在 listen 上
    listen {{SUB_PORT}} ssl http2;
    listen [::]:{{SUB_PORT}} ssl http2;
    server_name {{SUB_DOMAIN}};

    ssl_certificate     /etc/letsencrypt/live/{{SUB_DOMAIN}}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{{SUB_DOMAIN}}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    server_tokens off;
    root /var/www/sub;
    autoindex off;

    location ~ "^/s/([A-Za-z0-9]{32})$" {
        limit_req zone=sub burst=20 nodelay;
        set $token $1;
        add_header Cache-Control "no-store" always;
        add_header profile-update-interval "12" always;
        add_header Content-Disposition "attachment; filename*=UTF-8''{{PROFILE_NAME}}" always;
        add_header subscription-userinfo $sbm_userinfo always;
        default_type text/plain;
        try_files /$token/$sub_fmt =404;
    }

    # 用户专属使用说明页，与订阅同一个令牌目录（由 sbm 生成）
    location ~ "^/u/([A-Za-z0-9]{32})$" {
        limit_req zone=sub burst=20 nodelay;
        set $token $1;
        add_header Cache-Control "no-store" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header X-Frame-Options "DENY" always;
        charset utf-8;
        try_files /$token/guide.html =404;
    }

    # Web 管理界面（sbm-web 只监听 127.0.0.1:8090），密码文件由管理员用 htpasswd 维护
    location = /admin {
        return 301 /admin/;
    }

    location /admin/ {
        auth_basic "sbm admin";
        auth_basic_user_file /etc/nginx/sbm-admin.htpasswd;
        limit_req zone=admin burst=60 nodelay;
        proxy_pass http://127.0.0.1:8090/;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
        add_header X-Frame-Options "DENY" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "no-referrer" always;
    }

    location / {
        return 404;
    }
}

#!/bin/sh
set -eu

proxy_host="${SANDBOX_PROXY_HOST:-sandbox-egress-proxy}"
proxy_port="${SANDBOX_PROXY_PORT:-3128}"
proxy_ip="$(getent ahostsv4 "$proxy_host" | awk 'NR == 1 { print $1 }')"

if [ -z "$proxy_ip" ]; then
    echo "failed to resolve sandbox proxy: $proxy_host" >&2
    exit 1
fi

iptables -A OUTPUT \
    -m owner --uid-owner 20000-29999 \
    -p tcp -d "$proxy_ip" --dport "$proxy_port" \
    -j ACCEPT
iptables -A OUTPUT \
    -m owner --uid-owner 20000-29999 \
    -j REJECT

export SANDBOX_PROXY_URL="http://$proxy_ip:$proxy_port"
exec python /app/app.py

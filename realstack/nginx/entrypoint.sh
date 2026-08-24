#!/bin/sh
set -e
# 자체서명 인증서 생성(없을 때만). 키는 컨테이너 안에서만 생성/보관.
CERT_DIR=/etc/nginx/certs
if [ ! -f "$CERT_DIR/self.crt" ]; then
    mkdir -p "$CERT_DIR"
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
        -keyout "$CERT_DIR/self.key" -out "$CERT_DIR/self.crt" \
        -subj "/CN=localhost" >/dev/null 2>&1
fi
exec nginx -g 'daemon off;'

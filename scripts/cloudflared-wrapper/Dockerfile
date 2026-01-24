FROM alpine:3.18

RUN apk add --no-cache ca-certificates curl tar gzip \
  && mkdir -p /usr/local/bin /home/cloudflared

# Try to download the .tgz first (official releases provide a .tgz), fallback to raw binary
RUN set -eux; \
  if curl -fsSL -o /tmp/cloudflared.tgz "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.tgz"; then \
    tar -xzf /tmp/cloudflared.tgz -C /tmp || true; \
    if [ -f /tmp/cloudflared ]; then mv /tmp/cloudflared /usr/local/bin/cloudflared; fi; \
  else \
    curl -fsSL -o /usr/local/bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"; \
  fi; \
  chmod +x /usr/local/bin/cloudflared; \
  addgroup -g 1000 cloudflared 2>/dev/null || true; \
  adduser -D -u 1000 -G cloudflared cloudflared 2>/dev/null || true; \
  chown -R cloudflared:cloudflared /home/cloudflared /usr/local/bin/cloudflared

USER cloudflared
WORKDIR /home/cloudflared

ENTRYPOINT ["sh","-c"]

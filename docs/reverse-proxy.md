# Reverse proxy & EventSub

**You do not need any of this.** Without a public HTTPS URL, JellyTTV polls Twitch every 2 minutes
and detects go-live within that window using a single Helix request for all your channels. That is
perfectly fine for a home server.

Set up HTTPS only if you want **instant** go-live detection via EventSub webhooks.

## Why HTTPS is mandatory for EventSub

Twitch's rules for webhook transport are non-negotiable:

- The callback must be **HTTPS on port 443** with a certificate from a trusted CA. Self-signed
  certificates are rejected.
- Twitch POSTs a `webhook_callback_verification` challenge that must be echoed as the raw body
  within **10 seconds**.
- Every notification is signed with HMAC-SHA256 over
  `message_id + timestamp + raw_body`.

JellyTTV handles the challenge, signature verification, timestamp freshness and replay protection.
You only need to get the traffic to it.

After setting the public URL, enable **Settings → Use EventSub webhooks** and click **Reconcile**.
Check **Settings → URLs & go-live detection** — the badge should read *Webhook mode* and the
subscription count on the Diagnostics card should be `3 × number of channels`
(`stream.online`, `stream.offline`, `channel.update`).

Polling stays enabled as a safety net even in webhook mode.

---

## Option 1 — bundled Caddy (easiest)

Point a DNS A record at your host, open ports 80 and 443, then:

```bash
# .env
JELLYTTV_DOMAIN=jellyttv.example.com
CADDY_EMAIL=you@example.com
JELLYTTV_PUBLIC_BASE_URL=https://jellyttv.example.com
```

```bash
docker compose --profile with-caddy up -d
```

Caddy provisions and renews a Let's Encrypt certificate automatically. Config lives in
[`docker/Caddyfile`](../docker/Caddyfile).

## Option 2 — Cloudflare Tunnel (no open ports)

Good if your ISP blocks inbound 80/443 or you are behind CGNAT.

```yaml
# add to docker-compose.yml
  cloudflared:
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN}
```

In the Cloudflare Zero Trust dashboard, add a public hostname routing to `http://api:8730`.
Then set `JELLYTTV_PUBLIC_BASE_URL=https://jellyttv.example.com`.

> If you put Cloudflare Access in front of the hostname, **exclude `/eventsub/*`** — Twitch cannot
> authenticate through an Access login page. The callback is already protected by HMAC signature
> verification.

## Option 3 — Traefik

```yaml
  api:
    labels:
      traefik.enable: 'true'
      traefik.http.routers.jellyttv.rule: Host(`jellyttv.example.com`)
      traefik.http.routers.jellyttv.entrypoints: websecure
      traefik.http.routers.jellyttv.tls.certresolver: letsencrypt
      traefik.http.services.jellyttv.loadbalancer.server.port: '8730'
```

## Option 4 — nginx

```nginx
server {
    listen 443 ssl http2;
    server_name jellyttv.example.com;

    ssl_certificate     /etc/letsencrypt/live/jellyttv.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/jellyttv.example.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8730;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # HLS playlists and the SSE event feed must not be buffered.
        proxy_buffering    off;
        proxy_read_timeout 3600s;
    }
}
```

`proxy_buffering off` matters: without it the dashboard's live-update feed (`/api/events`) stalls
and HLS playlists arrive late.

---

## Two different URLs

JellyTTV has two base-URL settings and they are usually **not** the same value:

| Setting | Who calls it | Typical value |
|---|---|---|
| **JellyTTV base URL** | Jellyfin, on your LAN | `http://192.168.1.10:8730` or `http://api:8730` |
| **Public HTTPS URL** | Twitch, from the internet | `https://jellyttv.example.com` |

Keeping the Jellyfin-facing URL internal means your stream traffic never leaves the LAN or passes
through your tunnel — only the tiny EventSub webhooks do.

## Security notes

- `/tuner/*`, `/hls/*` and `/vod/*` require the tuner key. The HLS proxy additionally refuses to
  fetch any upstream host that is not a known Twitch/CDN domain, so it cannot be used as an open
  relay.
- `/eventsub/callback` is intentionally open but verifies the HMAC signature, rejects messages older
  than 10 minutes, and de-duplicates message ids.
- `/api/*` requires an admin session cookie.
- Twitch and Jellyfin credentials are encrypted at rest with a Fernet key in
  `CONFIG_ROOT/secret.key`. **Back that file up together with the database** — without it the stored
  secrets cannot be decrypted.

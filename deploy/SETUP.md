# Public access via domain — one-time VPS setup

Goal: reach the dashboard at **https://ig.debasisnishank.me** with HTTPS and a
password, without exposing the Flask app directly.

Architecture:

```
Browser ──HTTPS──> Caddy (:443, public) ──HTTP──> Flask/gunicorn (127.0.0.1:5000)
                     │
                     ├─ auto TLS cert (Let's Encrypt)
                     └─ basic auth (username + password) on every route
```

The Flask app stays bound to `127.0.0.1:5000`. Only Caddy is public.

---

## 1. DNS (Namecheap)

Domain List → `debasisnishank.me` → **Advanced DNS** → Add New Record:

| Type | Host | Value            | TTL       |
|------|------|------------------|-----------|
| A    | `ig` | `222.167.207.45` | Automatic |

Wait until it resolves before step 4 (Caddy needs it live to get a cert):

```bash
dig +short ig.debasisnishank.me     # should print 222.167.207.45
```

## 2. Open the firewall for web traffic

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
# Do NOT open 5000 — the app must stay private.
```

## 3. Install Caddy (Debian/Ubuntu)

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

## 4. Configure Caddy

Generate the password hash (you'll be prompted for the password twice):

```bash
caddy hash-password
# copy the $2a$... bcrypt hash it prints
```

Install the Caddyfile and paste your hash in place of `REPLACE_WITH_BCRYPT_HASH`:

```bash
sudo mkdir -p /var/log/caddy
sudo cp /opt/ig-analyzer/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile        # paste the bcrypt hash, set username if desired
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy fetches the TLS cert automatically on first request. Watch it:

```bash
sudo journalctl -u caddy -f
```

## 5. (Optional) Version-control the systemd unit

The `ig-dashboard` service already runs on the box. `deploy/ig-dashboard.service`
captures a production-ready version (gunicorn, localhost bind, hardening). Only
apply it if your current unit differs and you want to standardize:

```bash
sudo cp /opt/ig-analyzer/deploy/ig-dashboard.service /etc/systemd/system/ig-dashboard.service
sudo systemctl daemon-reload
sudo systemctl restart ig-dashboard
```

## 6. Verify

```bash
# Local app still healthy (no auth on localhost):
curl -s http://127.0.0.1:5000/api/v1/health

# Public URL requires the password (401 without, 200 with):
curl -s -o /dev/null -w '%{http_code}\n' https://ig.debasisnishank.me/
curl -s -u debasis:YOURPASSWORD https://ig.debasisnishank.me/api/v1/health
```

Then open **https://ig.debasisnishank.me** in a browser — you'll get a
username/password prompt, then the dashboard.

---

## Rotating / changing the password

```bash
caddy hash-password
sudo nano /etc/caddy/Caddyfile      # replace the hash
sudo systemctl reload caddy
```

## Switching to the .com domain

Add the same A record under `debasisnishank.com` (host `ig`), change the site
address at the top of `/etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`.

## Notes

- The bcrypt hash is safe to keep in git — it is not the password. But since this
  Caddyfile is committed, prefer keeping the real hash only in
  `/etc/caddy/Caddyfile` on the box and leaving the repo copy as the placeholder.
- Caddy renews the TLS cert automatically; there is no certbot cron to manage.

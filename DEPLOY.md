# Deployment guide (single VPS: systemd + Caddy)

This is the tested path. Everything below uses commands that exist in this repository. You need roughly an hour, plus waiting time for
third-party account approvals (Paystack business activation can take days; start it first).

## 0. What you must obtain (nobody can do these for you)
| Need | Why | Notes |
|---|---|---|
| A domain + DNS access | HTTPS and cookies | Create an `A` record `app.your-domain -> server IP` |
| A VPS (Ubuntu 24.04, 1 vCPU / 2 GB is plenty to start) | hosting | Keep SSH key-only |
| **Market data** | real prices | OANDA: a free *practice* account still serves real prices (create a token under *Manage API Access*). Optional backup: Twelve Data API key |
| **Paystack account** | payments | Live keys need business activation. Test keys work immediately |
| **SMTP provider** | verification emails, password resets, receipts, alerts | Any transactional provider. Configure SPF, DKIM and DMARC for your domain or mail lands in spam |
| **A lawyer** | terms, risk disclosure, data protection, licensing | See section 9. You must set `legal_reviewed: true` yourself |

## 1. Prepare the server
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv ufw caddy        # Caddy: see https://caddyserver.com/docs/install if not in your apt sources
sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
sudo adduser --system --group --home /opt/apex_trader apex
sudo mkdir -p /etc/apex /var/lib/apex /var/log/apex /var/backups/apex
sudo chown apex:apex /var/lib/apex /var/log/apex /var/backups/apex
```

## 2. Install the app
```bash
sudo unzip apex_trader.zip -d /opt && sudo chown -R apex:apex /opt/apex_trader
cd /opt/apex_trader && sudo -u apex python3 -m venv .venv && sudo -u apex .venv/bin/pip install -r requirements.txt
```

## 3. Generate your configuration
Prices are in **major units** (15000 = NGN 15,000); the tool stores kobo. Pick your own prices.
```bash
cd /opt/apex_trader && sudo -u apex .venv/bin/python -m apex.cli web-init --out /etc/apex/web.json \
  --domain app.your-domain --company "Your Legal Entity Ltd" --support-email support@your-domain \
  --jurisdiction "Federal Republic of Nigeria" --feed oanda --oanda-account YOUR-OANDA-ACCOUNT-ID \
  --fallback twelvedata --pro-price 15000 --elite-price 45000 --currency NGN
sudo chown root:apex /etc/apex/web.json && sudo chmod 640 /etc/apex/web.json
```
`web-init` lists what still blocks startup (normally only `legal_reviewed`). Edit `/etc/apex/web.json` for SMTP (`smtp_host`, `smtp_user`,
`smtp_from`) and anything else. Reference: `config/web.production.example.json`.

## 4. Secrets (never in the JSON file)
```bash
sudo cp /opt/apex_trader/.env.example /etc/apex/env && sudo chown root:root /etc/apex/env && sudo chmod 600 /etc/apex/env
sudo nano /etc/apex/env      # OANDA_TOKEN, SMTP_PASSWORD, TWELVEDATA_API_KEY; PAYSTACK_SECRET_KEY when you have it
```

## 5. Create your admin account (2FA is mandatory)
```bash
cd /opt/apex_trader && sudo -u apex .venv/bin/python -m apex.cli --config /etc/apex/web.json web-create-admin --email you@your-domain
```
Add the printed secret to an authenticator app and enter a code to confirm. Admin accounts can only be created here, never through the website.

## 6. TLS and the service
```bash
sudo cp /opt/apex_trader/deploy/Caddyfile /etc/caddy/Caddyfile && sudo sed -i 's/app.YOUR-DOMAIN/app.your-domain/' /etc/caddy/Caddyfile && sudo systemctl reload caddy
sudo cp /opt/apex_trader/deploy/apex-web.service /opt/apex_trader/deploy/apex-backup.service /opt/apex_trader/deploy/apex-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now apex-web apex-backup.timer
journalctl -u apex-web -f          # watch it start; Ctrl+C to leave
```
Run exactly **one** instance (rate limits and sessions' throttling live in memory; the database is SQLite).

## 7. Verify before announcing anything
```bash
cd /opt/apex_trader
sudo -u apex .venv/bin/python -m apex.cli --config /etc/apex/web.json news-check      # are the calendar and central-bank feeds reachable from THIS server?
sudo -u apex .venv/bin/python -m apex.cli --config /etc/apex/web.json web-check       # the same live checklist as Admin > Go-live checklist
curl -s https://app.your-domain/api/health/deep                                       # {"ok": true, ...}
```
Sign in as admin: the **Go-live checklist** at the top of the Admin page must show no failures. Warnings are things to finish.
Set `backup_dir` to `/var/backups/apex` (already done by `web-init`) so the checklist can verify backups are fresh.

## 8. Paystack (do this when your keys arrive)
1. Dashboard > Settings > API Keys & Webhooks. Copy the **test secret key** into `PAYSTACK_SECRET_KEY` in `/etc/apex/env`, then `sudo systemctl restart apex-web`.
2. Register the **Webhook URL** shown by `python -m apex.cli --config /etc/apex/web.json paystack-check` (`https://app.your-domain/webhooks/paystack`). Test and live modes have separate settings.
3. Run `paystack-check`: it validates the key with Paystack and prints what to register.
4. Make a **test payment** with a Paystack test card from a normal user account. Confirm: the plan activates, a receipt email arrives, the payment appears in Admin > Payments, replaying the webhook does not extend the plan twice.
5. Swap in the `sk_live_` key, restart, register the webhook in **live** mode, run `paystack-check` again (it should say LIVE), and do one real small payment.
6. Optional: copy Paystack's published webhook IP addresses into `paystack_webhook_ips` for a second layer of verification.

## 9. Things only you (and a lawyer) can confirm
* Terms, risk disclosure and privacy text on `/legal` are rendered from your settings and are a conventional starting point. Have a qualified lawyer review them for your country and your users' countries, then set `legal_reviewed: true`.
* Whether selling market analysis, signals or alerts requires registration or a licence where you and your customers are. Rules vary by country and are enforced.
* Data-protection obligations (registration, breach reporting, retention). The app provides data export and account deletion; you own the policy.
* Tax and Paystack merchant terms. The app never stores card data.

## 10. Operations
* **Backups**: the timer runs `deploy/backup.sh` daily at 02:30 UTC (14 kept). Copy them off the server: set `APEX_OFFSITE_CMD` in `/etc/apex/backup.env`, for example `APEX_OFFSITE_CMD='rclone copy "$1" remote:apex-backups/'`.
* **Restore**: `sudo systemctl stop apex-web`, copy a backup over `/var/lib/apex/apex_web.db` (owner `apex`, mode 600), `sudo systemctl start apex-web`, then check Admin > Audit log shows "hash chain intact".
* **Updates**: unzip the new release over `/opt/apex_trader` (keep `/etc/apex` and `/var/lib/apex`), `pip install -r requirements.txt`, `sudo systemctl restart apex-web`. Schema migrations run automatically.
* **Monitoring**: point an uptime monitor at `https://app.your-domain/api/health/deep` (returns 503 when the database or market data is unhealthy; weekends are handled). Watch `journalctl -u apex-web` and `/var/log/apex/`.
* **Logs**: the app rotates its own log (5 x 5 MB); `deploy/logrotate.apex` covers anything else.
* **Admin trading panel (optional)**: set `trading_config` to a file based on `config/trading.example.json`. Default is paper fills with an approval click; a live MetaTrader 5 broker needs `allow_live` plus the `APEX_I_UNDERSTAND_LIVE_RISK=yes` environment variable and should be proven on a demo account first.

## 11. Honest limits
* One process on SQLite suits hundreds of concurrent users, not tens of thousands. Plan a migration if you grow beyond that.
* Rate limiting and lockout counters reset when the process restarts.
* The OANDA, Twelve Data, Paystack and SMTP integrations follow each provider's documentation but could only be exercised against local look-alike servers while the code was written. `web-check`, `news-check` and `paystack-check` exist so you can prove each one from your own server before taking customers.
* Nothing in the app predicts markets or guarantees profit. Setup grades count satisfied criteria; they are not probabilities.

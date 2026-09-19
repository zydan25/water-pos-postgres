# Water POS server deployment

The current application is intentionally deployed **as-is on SQLite**.
PostgreSQL is not required by the running application at this stage.
The psycopg2 package is used by the separate SQLite-to-PostgreSQL migration helper.

## First install

```bash
bash deploy/init_server.sh
```

This creates a server-local `.env`, a virtual environment, Python
dependencies, Playwright Chromium, runtime folders, a fresh SQLite database
when missing, and the configured admin user.

## Reset the active database

```bash
bash deploy/reset_database.sh
```

The command requires typing `RESET`. It backs up the current SQLite database
under `backups/`, deletes the active database, recreates the schema and seed
data, creates the configured admin, and removes the historical bootstrap
account `zydan` on a fresh reset when a different username is configured.

## PM2

```bash
pm2 start deploy/ecosystem.config.cjs
pm2 save
pm2 status
pm2 logs water-pos-saif
```

The service binds to `127.0.0.1:5026`. One Gunicorn worker is deliberate
because `jobs.py` keeps job state in process memory.

## Nginx

```bash
sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
sudo cp deploy/nginx/saif.alattab.site.conf /etc/nginx/sites-available/saif.alattab.site
sudo ln -sf /etc/nginx/sites-available/saif.alattab.site /etc/nginx/sites-enabled/saif.alattab.site
sudo nginx -t
sudo systemctl reload nginx
```

Create the DNS A record for `saif.alattab.site` pointing to this server.

## TLS

```bash
sudo certbot --nginx -d saif.alattab.site
```

After HTTPS is active, set `FORCE_HTTPS=1` in the server-local `.env` and run:

```bash
pm2 restart water-pos-saif --update-env
```

Keep `.env`, `instance/secret.key`, SQLite databases, backups, and uploads
out of Git.

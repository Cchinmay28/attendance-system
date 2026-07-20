# 🏢 Office Attendance System

A full-stack Flask web app for employee clock-in/clock-out with role-based login and office network verification.

## 🚀 Setup & Run

```bash
pip install -r requirements.txt
python app.py
```
Open: http://localhost:5000

## 👤 Employee Login
| ID  | Password |
|-----|----------|
| 101 | pass101  |
| 102 | pass102  |
| 103 | pass103  |

## 🔑 Admin Login
| ID    | Password |
|-------|----------|
| admin | admin123 |

## ⚙️ Configuration (.env)
- `OFFICE_ALLOWED_IPS` — comma-separated allowed IPs or ranges; by default the app allows the office LAN range `192.168.0.0/24`.
  Local requests using `localhost` or `127.0.0.1` are allowed only when the host machine itself is connected to an allowed office network address.
- `TRUSTED_PROXY_IPS` — comma-separated proxy IPs that may safely set `X-Forwarded-For`; leave empty unless using a trusted reverse proxy.
- `PUBLIC_DEPLOYMENT=false` — set to `true` when the app is hosted on a public server or tunnel so the app trusts `X-Forwarded-For`/`X-Real-IP` headers from trusted proxies.
- `DEMO_MODE=false` — use real IP verification in production
- `ALLOW_LOCALHOST=false` — set to `true` to permit `127.0.0.1`/`::1` access only for local testing. Do not enable this in production unless you intentionally want localhost access regardless of office LAN connectivity.

> For public deployment, set `OFFICE_ALLOWED_IPS` to your office’s public outbound IP range instead of a private LAN range like `192.168.0.0/24`.

> Note: for real office-network enforcement, access the app through the host's LAN IP (for example `http://192.168.0.10:5000`), not `http://localhost:5000`, because `localhost` always resolves to loopback.
- `ADMIN_ID` / `ADMIN_PASSWORD` — admin credentials
- `SECRET_KEY` — Flask session secret
- `SUPABASE_URL` — your Supabase project URL
- `SUPABASE_SERVICE_ROLE_KEY` or `SUPABASE_ANON_KEY` — your Supabase API key

## 🗄️ Supabase Setup
1. Create a Supabase project.
2. Run the SQL from [supabase_schema.sql](supabase_schema.sql) in the SQL editor.
3. Add the environment variables above to your `.env` file.
4. Restart the Flask app.

When Supabase credentials are present, the app will use Supabase tables automatically. If they are absent, it falls back to local CSV files.

## 📁 CSV Files
- `employees.csv` — employee records
- `attendance_records.csv` — clock-in/out data
- `denied_attempts.csv` — failed access log

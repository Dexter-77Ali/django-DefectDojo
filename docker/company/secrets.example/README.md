# Secrets for docker-compose.production.yml

Copy this directory to `./secrets/` at the repository root (that path is gitignored), then
replace every value. One file per secret, no trailing newline needed.

| File | Used as | How to generate |
|------|---------|-----------------|
| `dd_secret_key` | Django `SECRET_KEY` | `openssl rand -base64 48` |
| `dd_credential_aes_256_key` | encryption key for stored tool credentials; never rotate after data exists | `openssl rand -base64 32` |
| `postgres_password` | password of the bundled Postgres role `defectdojo` | `openssl rand -base64 24` |
| `dd_database_url` | `postgresql://defectdojo:<postgres_password>@postgres:5432/defectdojo` | same password as above |
| `dd_company_ldap_bind_password` | read-only directory bind account (empty file when LDAP is off) | from your directory admins |
| `tls/nginx.crt`, `tls/nginx.key` | served by nginx on 443 | your CA, or `openssl req -x509 -newkey rsa:4096 -nodes -days 365 -subj "/CN=dojo.example.com" -keyout tls/nginx.key -out tls/nginx.crt` for a test host |

The files here are placeholders and must not be used as they are.

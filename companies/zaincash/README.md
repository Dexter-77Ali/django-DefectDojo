# ZainCash profile: what is in place, what ZainCash still has to supply

Everything below is wired and tested with placeholders (2026-09-23). Each item names the file it
goes into. Nothing here is secret; secrets go to `./secrets/` on the host that runs the stack
(see `docker/company/README.md`), never into git, email or chat.

## Verified so far

- Released images `ghcr.io/dexter-77ali/defectdojo-django:3.3.200-company.1` and
  `defectdojo-nginx:3.3.200-company.1` (public pull) run the production compose layer with file
  secrets, TLS and this profile: HTTPS login, branded dashboard, `DEBUG=False`, secure cookies,
  scheduled backups. Proven locally with a self-signed certificate.
- Directory login proven end to end against a test directory, over plain LDAP and over LDAPS
  with a private CA (login works with the CA certificate, is refused without it or with a wrong
  one). Active Directory uses the same code with `DD_COMPANY_LDAP_PROFILE=ad`.
- Fork CI green; security review closed.

## What we need from ZainCash

### Brand (marketing)

| # | Input | Format | Goes into |
|---|-------|--------|-----------|
| 1 | Main logo (login page, footer) | SVG, or PNG at least 1000 px wide, transparent background | `assets/logo.png`, `assets/login-logo.png` |
| 2 | Square symbol (sidebar at 32 px, browser tab) | SVG, or PNG at least 512 x 512 | `assets/icon.png`, `assets/favicon.png` |
| 3 | Brand colours | primary colour as hex (secondary if any), or the brand guidelines PDF | `DD_COMPANY_PALETTE` (we derive the ten shades) |

### Security policy (security team)

| # | Input | Goes into |
|---|-------|-----------|
| 4 | Product types (top-level grouping, for example one per business unit) and the applications under each | created in DefectDojo; names must match the AD groups in item 10 |
| 5 | Confirm the product fields and their choices: owner team, business unit (wallet, merchant-services, agent-network, core-banking, corporate-it), PCI DSS scope, data classification | `DD_COMPANY_FIELDS` |
| 6 | Days to fix per severity (critical, high, medium, low) and whether business-critical products get shorter deadlines (multiplier per product business criticality: very high, high, medium, low, very low) | SLA configuration in the UI; `DD_COMPANY_SLA_FACTORS` |
| 7 | Escalation mailbox (SOC distribution list) and which events go there (default: SLA breach, combined SLA breach, risk acceptance expiry) | `DD_COMPANY_ESCALATION_EMAILS`, `DD_COMPANY_ESCALATION_EVENTS` |

### IT and infrastructure

| # | Input | Goes into |
|---|-------|-----------|
| 8 | Mail relay: host, port, TLS mode (STARTTLS 587 or TLS 465), sender address (for example `defectdojo@zaincash.iq`), account if the relay needs one | `secrets/dd_email_url` on the server; sender also in System Settings |
| 9 | Active Directory: domain controller names reachable on LDAPS 636, the CA certificate that issued their LDAPS certificate (Base-64 `.cer`), user and group search base DNs | `DD_COMPANY_LDAP_SERVER_URI`, `_USER_BASE`, `_GROUP_BASE`; CA into `secrets/dd_company_ldap_ca_cert` |
| 10 | AD groups: one admin group (members become DefectDojo superusers), one group per product type named `dojo-pt-<product type name>`, and one test user in each group | `DD_COMPANY_LDAP_ADMIN_GROUP`; the group names themselves |
| 11 | Read-only AD service account: its DN, and its password typed into the server by ZainCash IT | `DD_COMPANY_LDAP_BIND_DN`; password into `secrets/dd_company_ldap_bind_password` |
| 12 | Server: a Linux VM with Docker Engine and the compose plugin (suggested start: 4 vCPU, 16 GB RAM, 100 GB SSD), or a Kubernetes namespace | `docker-compose.production.yml` or `helm/values-company.yaml` |
| 13 | DNS name for the service (for example `defectdojo.zaincash.iq`) and a TLS certificate with its private key for that name (PEM, full chain) | `DD_SITE_URL`, `DD_ALLOWED_HOSTS`; `secrets/tls/nginx.crt` and `nginx.key` |
| 14 | Firewall openings: users to the server on 443; server to the domain controllers on 636; server to the mail relay; server to `ghcr.io` on 443 for image pulls (or an internal registry mirror) | network |
| 15 | Off-host backup target (NFS share, object storage or a backup agent) and how many days to keep | `DD_COMPANY_BACKUP_DIR`, `DD_COMPANY_BACKUP_KEEP_DAYS` |
| 16 | Named administrators who receive the first local admin password (break-glass account) | handed over on first start |

Assumed until told otherwise: time zone `Asia/Baghdad` (`DD_TIME_ZONE` in `zaincash.env`).

## Engineering after the inputs arrive

1. Drop the artwork into `assets/`, set the values in `zaincash.env`, run
   `bash docker/company/apply-profile.sh zaincash`, tag a release (`3.3.N00-company.M`, workflow
   input `profile=zaincash`).
2. Fill `./secrets/` on the server from `docker/company/secrets.example/`, start the production
   layer, log in with a test user from each AD group.
3. Restore drill from the first backup.
4. Weekly upstream merge (Fork Strategy checklist); next upstream tag expected around 2026-09-28.

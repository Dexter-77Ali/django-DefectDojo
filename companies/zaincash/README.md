# ZainCash profile: what is in place, what ZainCash still has to supply

Everything below is wired and tested with placeholders (2026-09-23). Each open item names the
file it goes into and the check that proves it landed. Nothing here is secret; secrets go to
`./secrets/` on the host that runs the stack (see `docker/company/README.md`).

## Verified so far

- Released images `ghcr.io/dexter-77ali/defectdojo-django:3.3.200-company.1` and
  `defectdojo-nginx:3.3.200-company.1` (public pull) run the production compose layer with file
  secrets, TLS and this profile: HTTPS login, branded dashboard, `DEBUG=False`, secure cookies,
  scheduled backups. Proven locally with a self-signed certificate.
- Directory login proven end to end against a test OpenLDAP; the Active Directory profile is
  the same code with `DD_COMPANY_LDAP_PROFILE=ad`.
- Fork CI green; security review closed.

## Open items, owner ZainCash

| # | Input | Where it goes | Proof |
|---|-------|---------------|-------|
| 1 | Logo files: sidebar icon (square, shown at 32 px), main logo (login card and footer), favicon | `companies/zaincash/assets/icon.png`, `logo.png`, optional `favicon.png`, `login-logo.png`, `chop.png` (replaces the placeholders there) | `bash docker/company/apply-profile.sh zaincash`, build or tag a release with `profile=zaincash`, open the login page and the dashboard |
| 2 | Brand colours (10-step ramp, or the primary colour and we derive the ramp) | `DD_COMPANY_PALETTE` in `zaincash.env` | dashboard buttons and links take the colour; malformed JSON fails at container start |
| 3 | Confirm the four product fields and their choices (owner team, business unit and its five choices, PCI DSS scope, data classification) | `DD_COMPANY_FIELDS` in `zaincash.env` | `manage.py company_fields list` inside the uwsgi container |
| 4 | SLA policy: days per severity (set in the UI under SLA configuration) and the multiplier per product *Business criticality* | UI for the days; `DD_COMPANY_SLA_FACTORS` in `zaincash.env` for the multipliers (keys: very high, high, medium, low, very low) | a finding on a "very high" product shows the shortened SLA date |
| 5 | Escalation distribution list and which events (default: SLA breach, combined SLA breach, risk acceptance expiry) | `DD_COMPANY_ESCALATION_EMAILS`, `DD_COMPANY_ESCALATION_EVENTS` in `zaincash.env`; SMTP settings as upstream `DD_EMAIL_URL` | trigger an SLA breach on a test finding, check the mailbox |
| 6 | Active Directory: LDAPS host, read-only bind account (password as a file secret), user and group base DNs, admin group DN, one AD group per product type named `dojo-pt-<product type name>` | `DD_COMPANY_LDAP_*` in `zaincash.env` (set `DD_COMPANY_LDAP_ENABLED=True`), password in `secrets/dd_company_ldap_bind_password` | log in as a directory user: admin-group member is superuser, product-type group member sees that product type, everyone else sees nothing; the local `admin` still logs in |
| 7 | Hosting target (a Docker host, or a Kubernetes cluster), DNS name, TLS certificate for it | compose: `DD_SITE_URL`, `DD_ALLOWED_HOSTS`, `secrets/tls/nginx.crt` and `nginx.key`; Kubernetes: `helm/values-company.yaml` with `--set siteUrl --set host` | `docker compose $F up -d` then the login page over HTTPS; backups appear in the `defectdojo_backups` volume |
| 8 | Where backups are copied off the host | `DD_COMPANY_BACKUP_DIR` or a job that copies the volume | a restore drill from a dump |

## Engineering after the inputs arrive

1. Drop the artwork in `assets/`, set the values in `zaincash.env`, run `apply-profile.sh`, tag
   a release (`3.3.N00-company.M`, workflow input `profile=zaincash`).
2. One directory validation against the real AD with a test user in each group.
3. Deploy with the production compose layer (or `helm install`), then a restore drill.
4. Weekly upstream merge (`Fork Strategy` checklist); next upstream tag expected around 2026-09-28.

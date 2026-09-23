# Company deployment runbook

One build of this fork serves several companies. Everything company-specific is
configuration: environment variables (`DD_COMPANY_*`), file secrets, and the static
assets under `dojo/static/dojo/company/`. The mechanism is described in `CLAUDE.md`;
this file is the operator's checklist.

## Files

| File | Role |
|------|------|
| `docker-compose.yml` | upstream stack, never edited |
| `docker-compose.company.yml` | company image, `DD_COMPANY_*` variables with placeholder defaults, optional `ldap` test directory |
| `docker-compose.production.yml` | release mode: file secrets, TLS on 443, restart policies, prefork worker |
| `Dockerfile.company` + `requirements-company.txt` | company layer (LDAP libraries) on the upstream image |
| `docker/company/secrets.example/` | template for `./secrets/` |
| `docker/company/ldap/` | seed of the local test directory only |

## First deployment

```bash
F="-f docker-compose.yml -f docker-compose.company.yml -f docker-compose.production.yml"
cp -r docker/company/secrets.example secrets && $EDITOR secrets/*   # see secrets.example/README.md
export DJANGO_VERSION=3.3.200-company.1 NGINX_VERSION=3.3.200-company.1   # a tag published by company-release.yml
export DD_SITE_URL=https://dojo.example.com DD_ALLOWED_HOSTS=dojo.example.com
export DD_COMPANY_NAME="Example Corp"
docker compose $F pull
docker compose $F up -d
docker compose $F logs initializer | grep "Admin password:"   # first boot only; change it after login
```

Images come from the registry the release workflow pushes to: GitHub Packages of the fork
owner by default (`ghcr.io/<owner>/defectdojo-django:<tag>` is the company layer,
`<tag>-base` the upstream image, `defectdojo-nginx:<tag>` the static server). Point
`DD_COMPANY_IMAGE_DJANGO` and `DD_COMPANY_IMAGE_NGINX` at another registry path when needed.
To build locally instead: build the upstream image with `-f docker-compose.yml`, then the
company layer with `-f docker-compose.yml -f docker-compose.company.yml`.

## Company profiles

A profile is a folder `companies/<name>/` with `<name>.env` (non-secret `DD_COMPANY_*`
values) and `assets/` (`icon.png`, `logo.png` for the company layout; optional `favicon.png`,
`login-logo.png`, `chop.png` replacing the upstream images). Apply one with

```bash
bash docker/company/apply-profile.sh zaincash
docker compose --env-file companies/zaincash/zaincash.env $F up -d
```

Assets are baked into the images, so build (or tag a release) after applying a profile;
variables are read at container start. Secrets never go into a profile.

## Per-company configuration

| Variable | Effect | Default |
|----------|--------|---------|
| `DD_COMPANY_NAME` | sidebar, footer, report header, escalation subjects | `Company` |
| `DD_COMPANY_PALETTE` | JSON `{"50": "#…", …, "900": "#…"}`, recolours the UI at runtime | teal placeholder in `company.css` |
| `DD_COMPANY_FIELDS` | JSON field definitions (label, choices) for product metadata | owner-team, business-unit |
| `DD_COMPANY_SLA_FACTORS` | JSON product *Business criticality* (upstream product field: very high, high, medium, low, very low, none) → SLA day multiplier | 0.5 / 0.75 / 1.0 / 1.5 / 2.0 |
| `DD_COMPANY_ESCALATION_EMAILS`, `DD_COMPANY_ESCALATION_EVENTS` | escalation copies | none / SLA and risk-acceptance events |
| `DD_COMPANY_LDAP_*` | directory login (`DD_COMPANY_LDAP_ENABLED=True`, profile `ad` or `openldap`, URI, bases, admin group, group prefix) | off |
| `DD_FOOTER_VERSION` | version text in the footer | the image tag |

Logo files (`dojo/static/dojo/company/icon.png`, `logo.png`) and the upstream login logo and
favicon are baked into the images; per-company artwork means building the images from a
branch or tag that carries that company's files.

## LDAP against Active Directory

`DD_COMPANY_LDAP_PROFILE=ad`, `DD_COMPANY_LDAP_SERVER_URI=ldaps://dc.example.com:636`,
`DD_COMPANY_LDAP_BIND_DN=CN=svc-dojo,OU=Service Accounts,DC=example,DC=com` with the password
in `secrets/dd_company_ldap_bind_password`, `DD_COMPANY_LDAP_USER_BASE` and `_GROUP_BASE`,
`DD_COMPANY_LDAP_ADMIN_GROUP` (DN of the admin group). Create one AD group per product type
named `dojo-pt-<product type name>` (prefix configurable). Members of the admin group become
superusers; members of a product-type group see that product type; everyone else can log in
and sees nothing until a group is assigned. Local accounts (the bootstrap `admin`, break-glass
and service accounts, i.e. anything with a local password) keep working through the local
backend and are never matched by a directory account of the same name; a directory user
named `admin` is rejected by the directory backend. Deactivating a user in DefectDojo ends
their session on the next request.

## Backup and restore

```bash
docker compose $F exec -T postgres pg_dump -U defectdojo -Fc defectdojo > backup-$(date +%F).dump
docker compose $F exec -T postgres pg_restore -U defectdojo -d defectdojo --clean --if-exists < backup-YYYY-MM-DD.dump
```

Back up `./secrets/` separately; without `dd_credential_aes_256_key` stored tool credentials
cannot be decrypted. Uploaded files live in the `defectdojo_media` volume.

## Upgrade

Set `DJANGO_VERSION`/`NGINX_VERSION` to the new tag, `docker compose $F pull`, `docker compose $F up -d`.
The initializer applies migrations on every start. Downgrades are not supported by upstream migrations;
restore a backup instead.

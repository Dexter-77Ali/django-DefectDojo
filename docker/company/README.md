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
| `Dockerfile.company` + `requirements-company.txt` | company layer (LDAP libraries, compiled company translations) on the upstream image |
| `dojo/company/locale/` | company translation catalogs (`.po`; see *Language*) |
| `docker/company/secrets.example/` | template for `./secrets/` |
| `docker/company/ldap/` | seed of the local test directory only |

## First deployment

```bash
F="-f docker-compose.yml -f docker-compose.company.yml -f docker-compose.production.yml"
cp -r docker/company/secrets.example secrets && $EDITOR secrets/*   # see secrets.example/README.md
export DJANGO_VERSION=3.3.300-company.1 NGINX_VERSION=3.3.300-company.1   # a tag published by company-release.yml
export DD_SITE_URL=https://dojo.example.com DD_ALLOWED_HOSTS=dojo.example.com
export DD_COMPANY_NAME="Example Corp"
docker compose $F pull
docker compose $F up -d
docker compose $F logs initializer | grep "Admin password:"   # first boot only; change it after login
```

Images come from the registry the release workflow pushes to: GitHub Packages of the fork
owner by default (`ghcr.io/<owner>/defectdojo-django:<tag>` is the company layer,
`<tag>-base` the upstream image, `defectdojo-nginx:<tag>` the static server; the packages are
public, no login needed to pull). Point `DD_COMPANY_IMAGE_DJANGO` and `DD_COMPANY_IMAGE_NGINX`
at another registry path when needed. To build locally instead: build the upstream image with
`-f docker-compose.yml`, then the company layer with `-f docker-compose.yml -f docker-compose.company.yml`,
and build both again after every change: an image built earlier lacks `dojo/company/` and
`local_settings.py` and runs plain upstream without any error message. The production layer was
verified on 2026-09-30 from the `3.3.300-company.1` images: HTTPS login, branded dashboard,
`DEBUG` off, secure cookies, file secrets, directory login over LDAPS with the CA from
`secrets/dd_company_ldap_ca_cert`, scheduled backup.

**Do not deploy `3.3.200-company.1`.** It predates the LDAP security fixes (a directory account
named like a local account could log in as it; deactivated users kept their session) and has no
LDAPS CA support. Use `3.3.300-company.1` or later.

## Company profiles

A profile is a folder `companies/<name>/` with `<name>.env` (non-secret `DD_COMPANY_*`
values) and `assets/` (`icon.png`, `logo.png` for the company layout; optional `favicon.png`,
`login-logo.png`, `chop.png` replacing the upstream images). Apply one with

```bash
bash docker/company/apply-profile.sh zaincash
docker compose --env-file companies/zaincash/zaincash.env $F up -d
```

Assets are baked into the images, so build (or tag a release) after applying a profile;
variables are read at container start. Secrets never go into a profile. `docker compose
--env-file` only hands a variable to the containers when a compose file references it, so
`apply-profile.sh` (and the `profiles` job in company CI) refuses a profile that sets a
variable no compose file passes on.

## Per-company configuration

| Variable | Effect | Default |
|----------|--------|---------|
| `DD_COMPANY_NAME` | sidebar, footer, report header, escalation subjects | `Company` |
| `DD_COMPANY_PALETTE` | JSON `{"50": "#…", …, "900": "#…"}`, recolours the UI at runtime | teal placeholder in `company.css` |
| `DD_COMPANY_FIELDS` | JSON field definitions (label, choices) for product metadata | owner-team, business-unit |
| `DD_COMPANY_SLA_FACTORS` | JSON product *Business criticality* (upstream product field: very high, high, medium, low, very low, none) → SLA day multiplier | 0.5 / 0.75 / 1.0 / 1.5 / 2.0 |
| `DD_COMPANY_ESCALATION_EMAILS`, `DD_COMPANY_ESCALATION_EVENTS` | escalation copies | none / SLA and risk-acceptance events |
| `DD_TIME_ZONE` | local time for UI dates and the daily SLA job (07:30), e.g. `Asia/Baghdad` | `UTC` |
| `secrets/dd_email_url` | outgoing mail relay for notifications and escalation copies (production layer) | `smtp://localhost:25`, sends nothing |
| `DD_COMPANY_LDAP_*` | directory login (`DD_COMPANY_LDAP_ENABLED=True`, profile `ad` or `openldap`, URI, bases, admin group, product-type and team group prefixes) | off |
| `DD_COMPANY_UPLOAD_ENGAGEMENT_NAME` | engagement that UI uploads and CI re-imports share, so a fixed finding closes whichever way the next scan arrives | `Security scans` |
| `DD_COMPANY_SCAN_TYPES` | scanner allowlist, comma separated scan types, applied by `manage.py company_scan_types` (see *Scanner allowlist*) | empty: every scanner |
| `DD_COMPANY_RA_APPROVAL_REQUIRED` | only approvers (the security team) accept risk; everyone else requests it (see *Risk acceptance*) | `False` (ZainCash: `True`) |
| `DD_COMPANY_RA_DAYS` | durations a risk acceptance can be requested for, and extended by, comma separated days | `30,90,180` |
| `DD_COMPANY_MARK_FIXED` | *Mark fixed* on finding pages and in the findings bulk menu (see *Mark fixed*); `False` where developers may not close findings themselves | `True` |
| `DD_COMPANY_LDAP_RISK_APPROVER_GROUP` | group DN of the security team, whose members approve risk acceptance requests | workstation: `cn=dojo-security,…`; empty = managed with `company_teams --approver` |
| `DD_FOOTER_VERSION` | version text in the footer | the image tag |

Logo files (`dojo/static/dojo/company/icon.png`, `logo.png`) and the upstream login logo and
favicon are baked into the images; per-company artwork means building the images from a
branch or tag that carries that company's files.

## ZainCash setup (administrator checklist)

Configuration only, done once by a superuser after the first deployment. Nothing here needs a
build or a restart.

1. **Turn off unused features.** Configuration > System Settings: untick *Enable Calendar*,
   *Enable questionnaires*, *Enable Benchmarks*, *Enable checklists*, *Enable Product Tracking
   Files* and *Enable Endpoint Metadata Import*. Each one removes menu entries.
2. **Deduplication.** Same page: tick *Deduplicate findings*.
3. **Risk acceptance.** Same page: *Risk acceptance form default days* = `90` and
   *Risk acceptance expiration heads up days* = `10`. Never leave the heads-up field empty: the
   daily expiration job raises on an empty value (upstream `dojo/risk_acceptance/helper.py:295`).
4. **No one-click acceptance.** Keep *Enable simple risk acceptance* unticked on every product
   (product > Settings > Edit), so acceptance always goes through a request and the security team.
   With `DD_COMPANY_RA_APPROVAL_REQUIRED=True` only an approver can switch it on, but check products
   that had it on before the switch.
5. **Notifications.** Open `/notifications/system` and set *Other* and *Review requested* to Mail.
   Then open `/notifications/template` and set the same two rows there: every user created
   later, directory users on their first login included, copies the template row
   (`dojo/notifications/signals.py`).
6. **CI uploads.** Create a local service user (for example `ci-scanner`) that is neither staff nor
   superuser, and add it to each product type whose pipelines upload (the organization page >
   *Authorized Users* > *Add Users*; the product type must already exist). Log in as that user
   once and copy its key from the user menu > *API v2 Key* into the CI secret store. A pipeline then uploads with one
   call; the product and the engagement are created on the first upload, later uploads re-import
   into the same test and close the findings that disappeared:

   ```bash
   curl -fsS -X POST "https://<host>/api/v2/reimport-scan/" \
     -H "Authorization: Token $DEFECTDOJO_API_KEY" \
     -F auto_create_context=true \
     -F product_type_name="<product type>" \
     -F product_name="<product>" \
     -F engagement_name="Security scans" \
     -F test_title="<component>" \
     -F scan_type="<scanner, e.g. ZAP Scan>" \
     -F close_old_findings=true \
     -F file=@<report file>
   ```

   `engagement_name` must equal `DD_COMPANY_UPLOAD_ENGAGEMENT_NAME` (default `Security scans`) so UI
   uploads and CI share one history. `test_title` names the component (repository or service): each
   component gets its own test, so one pipeline never closes another component's findings.

   People upload from *Upload scan* in the sidebar (`/company/upload`), or with *Upload report* on a
   product page and *Upload new report* on a test page. That page writes to the same engagement, with
   its *Component* field as `test_title`; the full upstream import form stays behind *All options…*.

## Scanner allowlist

Upload pages list about 400 scanners. To show only the ones the company uses, set
`DD_COMPANY_SCAN_TYPES` to their exact names, comma separated (for example
`ZAP Scan,Trivy Scan,Semgrep JSON Report`; include every scanner a pipeline uploads), run the full
`up -d` so the containers receive it, then:

```bash
docker compose $F exec uwsgi python manage.py company_scan_types
docker compose $F restart uwsgi celeryworker   # the v2 API reads the scanner list at startup
```

The command deactivates every parser scan type that is not listed and has no tests, reactivates the
listed ones, and prints each change. Manual test types (such as *Pen Test*) and scanners that already
have tests are left as they are, so their history stays filterable. With the variable empty it changes
nothing. A deactivated scanner disappears from the upload pages and the API rejects it ("Parser … is
not active"); list it and run the command again, or tick it under Engagements > Test Types.

## LDAP against Active Directory

`DD_COMPANY_LDAP_PROFILE=ad`, `DD_COMPANY_LDAP_SERVER_URI=ldaps://dc.example.com:636`,
`DD_COMPANY_LDAP_BIND_DN=CN=svc-dojo,OU=Service Accounts,DC=example,DC=com` with the password
in `secrets/dd_company_ldap_bind_password`, `DD_COMPANY_LDAP_USER_BASE` and `_GROUP_BASE`,
`DD_COMPANY_LDAP_ADMIN_GROUP` (DN of the admin group). If the domain controllers' LDAPS certificate comes
from the internal enterprise CA (the usual case), put that CA certificate (PEM, "Base-64 encoded
X.509") into `secrets/dd_company_ldap_ca_cert`; the production layer points
`DD_COMPANY_LDAP_CA_CERT_PATH` at it. Left empty, the system CA store is used. Create one AD
group per product type named `dojo-pt-<product type name>` (prefix configurable). Members of the admin group become
superusers; members of a product-type group see that product type; everyone else can log in
and sees nothing until a group is assigned. Create one group per team named `dojo-team-<team>`
(`DD_COMPANY_LDAP_TEAM_GROUP_PREFIX`, default `dojo-team-`); its members form the team findings
are assigned to (see *Teams and assignments*). Set `DD_COMPANY_LDAP_RISK_APPROVER_GROUP` to the DN
of the security team's group: its members approve risk acceptance requests (see *Risk acceptance*).
The directory is authoritative: on every login a directory user's product types, teams and approver
role are set to exactly the groups they carry. Local accounts (the bootstrap `admin`, break-glass
and service accounts, i.e. anything with a local password) keep working through the local
backend and are never matched by a directory account of the same name; a directory user
named `admin` is rejected by the directory backend. Deactivating a user in DefectDojo ends
their session on the next request.

### Directory users before their first login

Directory users get a DefectDojo account on their first login, so until then nobody can pick them as
an assignee. `manage.py company_ldap_sync` reads the members of the admin, risk approver, `dojo-pt-*`
and `dojo-team-*` groups, creates or updates their accounts and applies the same product-type, team
and approver sync as a login (local accounts are never touched). Run it once at rollout and then nightly:

```bash
docker compose $F exec -T uwsgi python manage.py company_ldap_sync
# host crontab, nightly at 02:15:
15 2 * * * cd /opt/defectdojo && docker compose $F exec -T uwsgi python manage.py company_ldap_sync >> /var/log/defectdojo-ldap-sync.log 2>&1
```

It prints one line per member and a summary (`N directory group members: … created, … updated, …
inactive, … skipped; … rechecked, … deactivated`). Only direct members are read, as at login.
Directory users it did not reach are then rechecked: someone removed from every group loses their
product types, teams and admin rights; someone deleted from the directory, or disabled in Active
Directory, is deactivated (sessions and API token stop working; reactivate them by hand if they come
back). Directory users are recognised by the Django group `company:directory`, which every login and
sync sets, so people who already left before the first sync of this version are not caught:
deactivate those by hand. When no member can be synced at all (for example a wrong
`DD_COMPANY_LDAP_USER_BASE`), the recheck is skipped instead of deactivating everyone. Active Directory
returns at most 1500 members of a group per read; members past that are provisioned at their first login.

## Teams and assignments

Findings are assigned to a team and optionally a person: *Assign…* in the bulk menu of a findings
list or test page (tick findings first), or the *Assign* form under the tabs of a finding page.
The person list shows active team members who can open every selected finding. Each assignment
replaces the previous one, adds a note "Assigned to <team> / <person> by <user>" to every finding
and notifies the person, or every team member when no person is chosen (event
`company_finding_assigned`, delivered on the channels of the *Other* row; it also follows the
system notification settings). The assignment is stored as finding tags `team:<team>` and
`assignee:<username>`, so it survives close, reopen and re-import and filters on every findings list
and in the API (`?tags=team:wallet`). *My findings* and *My team's findings* in the sidebar and the
tiles on the dashboard (*My work*) open those lists.

Teams are Django groups named `team:<team>`. With LDAP they come from the `dojo-team-<team>`
groups. Without a directory, or for local accounts, a superuser manages them:

```bash
docker compose $F exec uwsgi python manage.py company_teams list
docker compose $F exec uwsgi python manage.py company_teams create wallet
docker compose $F exec uwsgi python manage.py company_teams add wallet alice
docker compose $F exec uwsgi python manage.py company_teams remove wallet alice
docker compose $F exec uwsgi python manage.py company_teams delete wallet   # findings keep their team:wallet tag
```

Team names are lower case: letters, digits, `.`, `_` and `-`. A directory user's teams are reset to
their directory groups on every login, so add directory users to the directory group instead. With
the system setting *Filter string matching* on, a *My team's findings* link for someone in several
teams matches nothing (that filter takes a single tag); keep the setting off.

## Mark fixed

*Mark fixed* under the tabs of an open finding's page, or *Mark fixed…* in the bulk menu of a findings
list or test page (tick findings first, then confirm), closes the findings as mitigated in one step:
upstream's close with the note "Marked fixed", the user as *Mitigated by*, now as the mitigation date,
endpoint or location statuses, JIRA and the `finding_closed` notification included. It needs edit
permission on every selected finding (otherwise 403 and nothing changes); findings closed already are
skipped. When active note types are marked mandatory, a single finding goes to upstream's close page,
which asks for those notes, and a bulk selection holding such a finding changes nothing. The assignment
tags stay on the finding, so when a later upload reports it again and re-import reactivates it, it is
back in the same person's *My findings*. `DD_COMPANY_MARK_FIXED=False` hides both buttons and refuses
the action, for companies where developers may not close findings themselves (upstream's *Close
Finding* stays, as permissions allow).

## Language (English / Arabic)

The user menu (top right) ends with *English* and *العربية*. A click saves the choice as the user's
language (the *Language* field of the profile, `UserContactInfo.language`) and sets the language
cookie, so the page reloads in that language; on another browser upstream's middleware picks the
saved choice up at the next sign-in. The other languages stay available in the profile form.
Upstream's own strings come from upstream's catalogs (`dojo/locale`); the company strings from
`dojo/company/locale/ar/LC_MESSAGES/django.po`, searched first (`LOCALE_PATHS` in
`local_settings.py`). Notes on findings and risk acceptances and notification texts are always written in
the instance language (`DD_LANGUAGE_CODE`, English), whatever language the acting user has chosen,
because other people read them.

The catalog is a first draft that a native Arabic speaker should review before Arabic is announced.
Only the `.po` file is in git (like upstream, `*.mo` is ignored); `Dockerfile.company` compiles it
with `msgfmt --check` into the image. After adding or changing a company string:

```bash
# extract: new strings arrive empty, changed ones marked fuzzy (needs gettext and Django: a throwaway container)
MSYS_NO_PATHCONV=1 docker run --rm -u root --entrypoint sh -v "$PWD:/app" -w /app/dojo/company \
  defectdojo/defectdojo-django:latest-company -c 'apt-get update -qq && apt-get install -y -qq gettext && \
  env -u DJANGO_SETTINGS_MODULE django-admin makemessages -l ar --add-location=file --no-obsolete -e html,py'
# translate the empty and fuzzy entries in django.po (remove the fuzzy flag), then compile for the dev stack:
MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/app" -w /app python:3.14-slim sh -c 'apt-get update -qq && \
  apt-get install -y -qq gettext && msgfmt --check -o dojo/company/locale/ar/LC_MESSAGES/django.mo dojo/company/locale/ar/LC_MESSAGES/django.po'
```

The dev stack bind-mounts the source, so it uses this locally compiled `django.mo` (upstream's own
catalogs are not compiled there, so upstream strings stay English in dev mode); restart uwsgi after
compiling. `unittests/company/test_polish.py` checks that every entry is translated and uses only the
placeholders of its English text. CI images are built from upstream's Dockerfiles without this step,
so no test asserts Arabic text.

## Risk acceptance

Members request, the security team approves. *Request risk acceptance* under the tabs of a finding
page, or *Request risk acceptance…* in the bulk menu of a findings list or test page (tick findings
first), opens one page: the findings, a required justification and the duration
(`DD_COMPANY_RA_DAYS`, default 30, 90 or 180 days; the system setting *Risk acceptance form default
days* is preselected when it is one of them). Findings that are closed, already accepted, already
waiting, or on a product with *Enable full risk acceptance* off are listed and left out. The findings
stay open; each gets a note, and the approvers who can open those products are notified (event
`company_risk_requested`, which also goes to the escalation mailbox when it is listed in
`DD_COMPANY_ESCALATION_EVENTS`). The finding page then shows *Pending approval*.

*Risk acceptance* in the sidebar opens the inbox, with the number of requests waiting for an approver:

- *Waiting for my approval* (approvers only): each request opens to its findings with severity and
  links, then *Approve*, or *Reject* with a reason. Tick several and *Approve selected* to approve in
  bulk. Nobody decides their own request, and an approver must be able to edit every finding of it.
- *My requests*: status, decision, note and the risk acceptances an approval created; *Cancel request*
  while it waits.
- *Active* and *Expiring in 30 days*: lists of risk acceptances in the products the user can see. In
  *Expiring in 30 days*, whoever may edit the engagement can *Let expire* an acceptance now (upstream's
  expire: its findings reopen when it reactivates them, and a note records who did it), and approvers
  can *Extend* it by one of the `DD_COMPANY_RA_DAYS` durations, counted from its current expiry (or
  from today when that has passed). Extending clears the expiry warning, so the heads-up goes out again
  before the new date, and adds a note "… extended this risk acceptance (reason: by N days, until
  <date>)". Both need edit permission on the engagement; with enforcement on, members still cannot
  extend through any other path either.

Approving creates one upstream risk acceptance per engagement: owner the requester, *Accepted by* the
approver, decision *Accept*, the justification as decision details, expiring after the requested days
(counted from the approval); the findings become risk accepted with upstream's notes and JIRA updates.
Rejecting keeps the findings open and records the reason on them. The requester is notified either way
(`company_risk_decided`), and the expiry notices of an acceptance (`risk_acceptance_expiration`) also
reach its owner. A decided request never changes, so the inbox is the audit trail: who asked, why, for
how long, who decided and when. The dashboard shows *My risk requests* and, for approvers, *Waiting for
my approval*.

**Approvers** hold the permission `company.approve_riskacceptancerequest`: members of the Django group
`risk-approvers` and superusers (staff alone is no approver). With LDAP, `DD_COMPANY_LDAP_RISK_APPROVER_GROUP` (the security
team's group DN) fills that group on every login and every `company_ldap_sync`; the role is never
granted through staff, which would also manage every product and user. Local accounts:

```bash
docker compose $F exec uwsgi python manage.py company_teams --approver sam
docker compose $F exec uwsgi python manage.py company_teams --no-approver sam
docker compose $F exec uwsgi python manage.py company_teams list   # last line: the approvers
```

**Enforcement** (`DD_COMPANY_RA_APPROVAL_REQUIRED=True`, on in the ZainCash profile, off by default).
For a signed-in user who is no approver, every upstream way to accept risk is refused with a 403, in
the UI and in API v2: adding a risk acceptance, adding findings to one, changing its decision, *Accepted
by*, owner, *Reactivate findings on expiration* or its expiry handling, moving its expiry later,
reinstating an expired one, simple (one-click) acceptance, switching simple acceptance on for a product,
and copying an engagement that holds risk acceptances (refused before anything is copied). A finding
they save as risk accepted that was not accepted before is saved open instead: scan import and re-import
take "risk accepted" from some reports (Threagile, Invicti/Netsparker and others), and copied findings
and tests keep it. Upstream's *Add Risk Acceptance* links are hidden for them, and its add, edit,
reinstate and delete pages answer 403. They can still *Expire Now* an acceptance and, in API v2,
remove findings from one or delete it (on purpose: removing every finding has the same effect): all
of these make findings active again; editing its name or notes stays open too. The celery expiry job,
management commands and the JIRA webhook are not affected. The variable needs the full `up -d`.

## Backup and restore

The production layer runs a `backup` service: every `DD_COMPANY_BACKUP_INTERVAL` seconds
(default daily) it writes `defectdojo-<stamp>.dump` (pg_dump custom format) and
`media-<stamp>.tgz` (uploaded files) into `DD_COMPANY_BACKUP_DIR` (a host path, or the
`defectdojo_backups` volume by default) and deletes files older than
`DD_COMPANY_BACKUP_KEEP_DAYS` (14). Copy that directory off the host; the service does not.

```bash
docker compose $F logs --tail 5 backup                                              # last backups written
docker compose $F run --rm -v "$PWD:/out" --entrypoint sh backup -c 'cp /backups/* /out/'   # fetch from the volume
docker compose $F exec -T postgres pg_restore -U defectdojo -d defectdojo --clean --if-exists < defectdojo-<stamp>.dump
docker run --rm -v defectdojo_defectdojo_media:/media -v "$PWD:/in:ro" alpine sh -c 'rm -rf /media/* && tar xzf /in/media-<stamp>.tgz -C /media'
```

The media volume is named `<compose project>_defectdojo_media` (`docker volume ls` shows it);
stop the stack before restoring into it. A manual dump at any time:
`docker compose $F exec -T postgres pg_dump -U defectdojo -Fc defectdojo > backup-$(date +%F).dump`.

Back up `./secrets/` separately; without `dd_credential_aes_256_key` stored tool credentials
cannot be decrypted.

## Upgrade

Set `DJANGO_VERSION`/`NGINX_VERSION` to the new tag, `docker compose $F pull`, `docker compose $F up -d`.
The initializer applies migrations on every start, the company app's own included
(`dojo/company/migrations/`, first `0001_initial`: the risk acceptance requests table). Downgrades are
not supported by upstream migrations; restore a backup instead.

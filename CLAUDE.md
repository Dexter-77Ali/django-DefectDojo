@AGENTS.md

# Company fork guide

This checkout is the company fork of DefectDojo. The upstream development guide imported above still governs upstream files and upstream contributions; this file adds the fork's own rules. The long-form codebase map lives in the Obsidian vault at `E:\obsisian\hussie\DefectDojo\` (start at `DefectDojo MOC.md`; every subsystem note lists entry points with merge risk).

## Fork facts

- Remotes: `origin` = `Dexter-77Ali/django-DefectDojo` (the fork), `upstream` = `DefectDojo/django-DefectDojo`.
- `master` is a fast-forward-only mirror of `upstream/master`. Never commit on it; the checked-in branch guard blocks edits there.
- `company/main` carries all company work. Topic branches are cut from and merged back into `company/main`. The upstream `dev`/`bugfix` branch check in the imported guide only matters when preparing an upstream contribution.
- Base version 3.3.1. Every upstream tag (monthly `x.y.0`, weekly `x.y.N00`) is merged into `company/main` as a real merge commit; see the release-merge checklist in the vault's `Fork Strategy.md`.
- The company build id is the image tag (`3.3.1-company.N`) plus `DD_FOOTER_VERSION`. The three version files (`dojo/__init__.py`, `components/package.json`, `helm/defectdojo/Chart.yaml`) are never edited.

## Never edit (conflicts on every upstream merge)

`dojo/settings/settings.dist.py`, `dojo/models.py` and every domain `models.py`, `dojo/db_migrations/`, `dojo/templates/base.html`, `dojo/api_v2/*`, `docker-compose.yml` and `docker-compose.override.*.yml`, `Dockerfile.*`, `nginx/*.conf`, `helm/defectdojo/*`, the three version files, `AGENTS.md`, `.github/workflows/*` (upstream ones), `LICENSE.md`, `NOTICE`.

If a change truly must land in one of these: smallest possible diff, marked with a `# company:` comment, and try to upstream it first.

## Where company changes go

- **Settings**: `dojo/settings/local_settings.py`, committed (add `!dojo/settings/local_settings.py` to `.gitignore` when the file is created). It runs after `settings.dist.py` in the same namespace via django-split-settings: mutate existing structures (`INSTALLED_APPS += (...)`, `MIDDLEWARE.insert(...)`, `CELERY_BEAT_SCHEDULE[...]`, `_DOJO_TEMPLATE_DIRS.insert(0, ...)`), never re-declare them. The file is exec'd, so use absolute imports. Values travel as `DD_*` environment variables, secrets as `DD_*_FILE`; nothing secret in git.
- **Code**: the company Django app is `dojo/company/` (label `company`, `CompanyConfig` in `dojo/company/apps.py`), chosen 2026-09-19 because it ships through the existing `COPY dojo/` in both images. It gets its own `migrations/` directory when the first model appears and an `AppConfig.ready()` for signals and hook assertions. If an upstream meta-test that scans `dojo/` ever flags it, exempt the package there rather than moving it.
- **URLs**: `ROOT_URLCONF = "company.urls"` in `local_settings.py`; `company/urls.py` re-exports `handler400/403/500` from `dojo.urls` and appends `dojo.urls.urlpatterns`. `PRELOAD_URL_PATTERNS` / `EXTRA_URL_PATTERNS` are for view-free patterns only (importing views there raises `AppRegistryNotReady`).
- **Templates**: company template directory inserted at index 0 of `_DOJO_TEMPLATE_DIRS`; a company `base.html` uses recursive `{% extends "base.html" %}` and overrides only the blocks `header_logo`, `footer`, `support-tab`, `dojo_css`. Leaf templates (login, reports, notification `.tpl`) are overridden by mirroring their relative path.
- **Static**: `dojo/static/dojo/company/`, the only location both the dev-mode nginx mount and the nginx image pick up.
- **Data**: `DojoMeta` metadata and tags first; typed columns only as models in the company app with `ForeignKey`/`OneToOneField` to `"dojo.<Model>"`. Never a migration under `dojo/db_migrations`.
- **Hooks that exist for this**: `NOTIFICATION_MANAGER`, `JIRA_CONNECT_METHOD`, `CELERY_TASK_CONTEXT_MANAGERS`, `PARSER_TEST_CLASS_PATH`, the URL hooks above, and the `get_custom_method` registry in `dojo/utils.py:2254` (`FINDING_SLA_PERIOD_METHOD`, `FINDING_SLA_EXPIRATION_CALCULATION_METHOD`, `FINDING_DEDUPE_METHOD`, `FINDING_DEDUPE_BATCH_METHOD`, `FINDING_FALSE_POSITIVE_HISTORY_CANDIDATE_FILTER_METHOD`, `BULK_DELETE_FINDINGS_METHOD`, `FINDING_HASH_METHOD`, `FINDING_COMPUTE_HASH_METHOD`). Dotted paths whose module is missing fail silently: assert every configured hook in `AppConfig.ready()`.
- **Containers**: `docker-compose.company.yml` selected with `-f` or `COMPOSE_FILE`; `Dockerfile.company` (FROM the upstream-built image) only when extra pip packages (`requirements-company.txt`) or CA certificates are needed.
- **Tests and CI**: new files only, `unittests/company/` (with `__init__.py`) and `tests/company_*_test.py`; `.github/workflows/company-ci.yml` that `uses:` upstream's reusable workflows (`ruff.yml`, `build-docker-images-for-testing.yml`, `rest-framework-tests.yml`, `integration-tests.yml`). Upstream's `unit-tests.yml` never triggers for `company/*` branches.
- **Do not revert upstream removals** (SSO moved to Pro in 3.0; Tool Configuration, API pull parsers and dbbackup leave in 3.5). Add replacements as company code.

## Subsystem map

| Vault note | Key paths |
|------------|-----------|
| Configuration and Environment | `dojo/settings/settings.py` (split_settings), `settings.dist.py:40-315` (DD_* schema), `docker/extra_settings/`, `docker/secret-file-loader.sh` |
| Deploy Build and Release | `Dockerfile.django-debian`, `Dockerfile.nginx-alpine`, `docker-compose.yml` + overrides, `docker/setEnv.sh`, `helm/defectdojo/`, `.github/workflows/release-*` |
| Authentication and Authorization | `settings.dist.py:552` (backends: local accounts only in OSS 3.x), `dojo/authorization/authorization.py`, `dojo/authorization/query_filters.py`, `dojo/middleware.py:67` |
| Integrations and Celery | `dojo/notifications/helper.py:45`, `dojo/celery.py`, `dojo/celery_dispatch.py`, `dojo/jira/services.py`, `dojo/apps.py` |
| Data Model and Migrations | `dojo/models.py` (re-export hub), `dojo/<domain>/models.py`, `dojo/db_migrations/`, `dojo/auditlog/services.py:135`, `scripts/check_migration_leaves.py` |
| REST API v2 and v3 | `dojo/urls.py:105-172`, `dojo/api_v2/views.py`, `dojo/api_v3/api.py`, `dojo/<module>/api_v3/routes.py` (router factories) |
| Tests and CI | `run-unittest.sh`, `docker/entrypoint-unit-tests.sh`, `unittests/dojo_test_case.py`, `.github/workflows/unit-tests.yml` |
| Branding and UI | `dojo/templates/base.html:161,962`, `dojo/templates/dojo/login.html`, `dojo/static/dojo/img/`, `components/tailwind.css`, `dojo/context_processors.py` |
| Reports SLA and Notifications | `dojo/reports/ui/views.py:353`, `dojo/models.py:238` (SLA_Configuration), `dojo/finding/models.py:1152`, `dojo/utils.py:2254`, `settings.dist.py:1034` (beat schedule) |
| Parsers | `dojo/tools/<name>/parser.py`, `dojo/tools/factory.py`, `unittests/test_parsers.py`, skill `defectdojo-parser` |
| Fork Strategy | decisions, never-edit list, preferred mechanisms, release-merge checklist, merge log |

## Running and testing on this machine

Windows 10, Docker Desktop (WSL 2 backend, disk image on E:), Git Bash, no Python on the host. Docker Desktop does not auto-start.

- The stack runs in dev mode: `docker compose up -d` (UI http://localhost:8080, mailhog http://localhost:8025, Postgres on 5432). `docker-compose.override.yml` is a plain copy of the dev override because Git Bash `ln -s` copies; `docker/setEnv.sh` therefore always reports release mode. Switch modes by passing `-f docker-compose.yml -f docker-compose.override.<mode>.yml` explicitly, or `rm docker-compose.override.yml` to return to release mode.
- Source is bind-mounted at `/app`; uwsgi reloads on `.py` changes, templates re-render per request, static files under `dojo/static/dojo` are served live.
- Single test: `bash ./run-unittest.sh -t unittests.company.test_x.TestX -f`, or `docker compose exec -T uwsgi python manage.py test <dotted.path> --keepdb -v2`. CI-equivalent full run uses the `unit_tests_cicd` override (see the Tests and CI note). The shipped skills `defectdojo-dev` (test loop, API token) and `defectdojo-parser` apply.
- `manage.py`, migrations and `scripts/check_migration_leaves.py` run inside the uwsgi container only.
- Lint: `MSYS_NO_PATHCONV=1 docker run --rm -v /e/defectdojo:/app -w /app python:3.14-slim sh -c "pip install -q ruff==0.16.5 && ruff check ."`
- Line endings: this clone has `core.autocrlf=false` and `core.eol=lf`; the system gitconfig says `autocrlf=true`, so never re-clone without `--config core.autocrlf=false` or the container entrypoints break.
- Upstream sync:

```bash
git fetch upstream --tags
git checkout master && git merge --ff-only upstream/master && git push origin master
git checkout company/main && git merge <tag>
docker compose build && docker compose up -d
docker compose exec uwsgi python scripts/check_migration_leaves.py
```

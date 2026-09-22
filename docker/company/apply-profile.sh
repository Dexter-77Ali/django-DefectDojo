#!/usr/bin/env bash
# company: apply a company profile from companies/<name>/ to this checkout.
#
#   bash docker/company/apply-profile.sh zaincash
#
# Copies the profile's artwork into the static tree (icon.png and logo.png for the company
# layout; favicon.png, login-logo.png and chop.png replace the upstream images when present)
# and prints the compose invocation that loads the profile's variables. Assets are baked into
# the images, so build after applying a profile; variables are read at container start.
set -euo pipefail

name="${1:?usage: apply-profile.sh <company>}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
profile="$root/companies/$name"
[ -d "$profile" ] || { echo "no profile at companies/$name" >&2; exit 1; }

assets="$profile/assets"
company_static="$root/dojo/static/dojo/company"
upstream_img="$root/dojo/static/dojo/img"

for f in icon.png logo.png; do
    [ -f "$assets/$f" ] && cp "$assets/$f" "$company_static/$f" && echo "static: dojo/static/dojo/company/$f"
done
[ -f "$assets/favicon.png" ] && cp "$assets/favicon.png" "$upstream_img/favicon.png" && echo "static: dojo/static/dojo/img/favicon.png (browser icon, sidebar fallback)"
[ -f "$assets/login-logo.png" ] && cp "$assets/login-logo.png" "$upstream_img/logo.png" && echo "static: dojo/static/dojo/img/logo.png (login card)"
[ -f "$assets/chop.png" ] && cp "$assets/chop.png" "$upstream_img/chop.png" && echo "static: dojo/static/dojo/img/chop.png (upstream footer mark)"

env_file="$profile/$name.env"
[ -f "$env_file" ] || { echo "no $env_file" >&2; exit 1; }
cat <<EOF

Profile '$name' applied. Run the stack with its variables:

  docker compose --env-file companies/$name/$name.env \\
    -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.company.yml up -d

Production: replace the dev override with docker-compose.production.yml and keep secrets in ./secrets/.
EOF

#!/usr/bin/env bash
# company: apply a company profile from companies/<name>/ to this checkout.
#
#   bash docker/company/apply-profile.sh zaincash
#
# Copies the profile's artwork into the static tree (icon.png and logo.png for the company
# layout; favicon.png and login-logo.png, when present, replace upstream's browser icon and login
# logo, written as both PNG and the SVG that upstream pages show; chop.png is still copied but no
# page shows it since 3.4.0) and prints the compose invocation that loads the profile's variables. Assets are baked into
# the images, so build after applying a profile; variables are read at container start.
set -euo pipefail

name="${1:?usage: apply-profile.sh <company>}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
profile="$root/companies/$name"
[ -d "$profile" ] || { echo "no profile at companies/$name" >&2; exit 1; }
env_file="$profile/$name.env"
[ -f "$env_file" ] || { echo "no $env_file" >&2; exit 1; }

# Every variable the profile sets must be passed to the containers by a compose file;
# `docker compose --env-file` silently drops the others. Checked before anything is copied.
missing=0
for var in $(grep -oE '^DD_[A-Z0-9_]+' "$env_file"); do
    grep -q "\${$var[:}]" "$root/docker-compose.company.yml" "$root/docker-compose.production.yml" \
        || { echo "$var is set by the profile but no compose file passes it to the containers" >&2; missing=1; }
done
[ "$missing" = 0 ] || exit 1

assets="$profile/assets"
company_static="$root/dojo/static/dojo/company"
upstream_img="$root/dojo/static/dojo/img"

# Since 3.4.0 upstream pages show SVG artwork (favicon.svg in every page head, logo.svg on the login card), with the PNGs only as
# fallback or for the report cover. Wrap the PNG in an SVG of the same size: an SVG loaded as an image or icon loads no external
# file, so the PNG travels inside as a data: URI. unittests/company/test_upstream_contract.py fails when upstream renames these.
png_to_svg() {
    case "$(od -An -tx1 -N8 "$1" | tr -d ' \n')" in
        89504e470d0a1a0a) ;;
        *) echo "$1 is not a PNG" >&2; exit 1 ;;
    esac
    local w h
    # width and height: the first two big-endian 32-bit fields of the IHDR chunk
    read -r w h < <(od -An -tu1 -j16 -N8 "$1" | awk '{print $1*16777216+$2*65536+$3*256+$4, $5*16777216+$6*65536+$7*256+$8}')
    printf '<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s"><image width="%s" height="%s" href="data:image/png;base64,%s"/></svg>\n' \
        "$w" "$h" "$w" "$h" "$w" "$h" "$(base64 < "$1" | tr -d '\n')" > "$2"
}

for f in icon.png logo.png; do
    [ -f "$assets/$f" ] && cp "$assets/$f" "$company_static/$f" && echo "static: dojo/static/dojo/company/$f"
done
if [ -f "$assets/favicon.png" ]; then
    cp "$assets/favicon.png" "$upstream_img/favicon.png"
    png_to_svg "$assets/favicon.png" "$upstream_img/favicon.svg"
    echo "static: dojo/static/dojo/img/favicon.svg and favicon.png (browser icon)"
fi
if [ -f "$assets/login-logo.png" ]; then
    cp "$assets/login-logo.png" "$upstream_img/logo.png"
    png_to_svg "$assets/login-logo.png" "$upstream_img/logo.svg"
    echo "static: dojo/static/dojo/img/logo.svg (login card) and logo.png (report cover page)"
fi
[ -f "$assets/chop.png" ] && cp "$assets/chop.png" "$upstream_img/chop.png" && echo "static: dojo/static/dojo/img/chop.png (not shown by any page since 3.4.0)"

cat <<EOF

Profile '$name' applied. Run the stack with its variables:

  docker compose --env-file companies/$name/$name.env \\
    -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.company.yml up -d

Production: replace the dev override with docker-compose.production.yml and keep secrets in ./secrets/.
EOF

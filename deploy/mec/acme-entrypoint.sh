#!/bin/sh
# ACME CSE for ANCHOR in the MEC Sandbox, HTTPS only, through the team's secure adapter (anchor.acme_tls).
# ROLE selects acme/<ROLE>.ini.tmpl. PUBLIC_HOST/HTTP_PORT form the CSE poa.
set -eu
. /usr/local/lib/anchor/tls-env.sh
sed -e "s#__PUBLIC_HOST__#${PUBLIC_HOST:-localhost}#" -e "s#__HTTP_PORT__#${HTTP_PORT:-8080}#" \
    -e "s#__REGISTRAR_URL__#${REGISTRAR_URL:-https://127.0.0.1:9}#" \
    -e "s#__CSE_ID__#${CSE_ID:-id-edge}#" -e "s#__CSE_NAME__#${CSE_NAME:-cse-edge}#" \
    "/etc/anchor/${ROLE:?}.ini.tmpl" > /var/lib/acme/acme.ini
cd /var/lib/acme
exec python -m anchor.acme_tls --headless --base-directory /var/lib/acme --config acme.ini

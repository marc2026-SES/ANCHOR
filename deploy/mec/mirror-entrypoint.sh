#!/bin/sh
# Read-only copier: real ship CSE (SOURCE_SHIP_URL, verified with SOURCE_CA_B64) -> sandbox ship mirror (SHIP_CSE_URL,
# verified with the sandbox CA). It creates nothing on the real ship. The ship relay owns the mirror's resources.
set -eu
. /usr/local/lib/anchor/tls-env.sh
export SHIP_CA="$ANCHOR_TLS_DIR/ca.crt"
if [ -n "${SOURCE_CA_B64:-}" ]; then
  export SOURCE_CA="$ANCHOR_TLS_DIR/source-ca.crt"
  python -c "import base64,os;s=os.environ['SOURCE_CA_B64'];open(os.environ['SOURCE_CA'],'wb').write(base64.b64decode(s+'='*(-len(s)%4)))"
fi
exec python /usr/local/bin/ship_link.py

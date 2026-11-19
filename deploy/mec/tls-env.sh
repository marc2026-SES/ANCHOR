# Sourced by the MEC entrypoints. Writes the sandbox TLS files from scenario env
# (base64, padding optional: the scenario env format is K=V,K=V) into $ANCHOR_TLS_DIR.
#   TLS_CA_B64      sandbox CA (verifies every sandbox CSE/core)
#   TLS_CERT_B64    server certificate of this pod
#   TLS_KEY_B64     its key
export ANCHOR_TLS_DIR=${ANCHOR_TLS_DIR:-/var/lib/anchor-tls}
mkdir -p "$ANCHOR_TLS_DIR" && chmod 700 "$ANCHOR_TLS_DIR"
b64file() { python -c "import base64,os,sys;s=os.environ[sys.argv[1]];open(sys.argv[2],'wb').write(base64.b64decode(s+'='*(-len(s)%4)))" "$1" "$2"; }
b64file TLS_CA_B64 "$ANCHOR_TLS_DIR/ca.crt"
b64file TLS_CERT_B64 "$ANCHOR_TLS_DIR/server.crt"
( umask 077; b64file TLS_KEY_B64 "$ANCHOR_TLS_DIR/server.key" )
export REQUESTS_CA_BUNDLE="$ANCHOR_TLS_DIR/ca.crt"

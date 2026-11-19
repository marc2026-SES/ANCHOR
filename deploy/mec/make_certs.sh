#!/bin/sh
# Sandbox-only TLS material for the MEC deployment (never the real Pi CA).
# Writes to $OUT (default ~/anchor-secrets/sandbox-tls), outside the repository:
#   ca.crt / ca.key        "ANCHOR MEC Sandbox CA"
#   server.crt / server.key  one certificate for every sandbox CSE and core
# make_scenario.py embeds ca.crt, server.crt and server.key into the scenario env.
set -eu
OUT=${OUT:-$HOME/anchor-secrets/sandbox-tls}
NODE_IP=${NODE_IP:-100.96.155.4}
LAN_IP=${LAN_IP:-172.19.30.16}
mkdir -p "$OUT" && chmod 700 "$OUT" && cd "$OUT"
[ -f ca.key ] || openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=ANCHOR MEC Sandbox CA" \
  -keyout ca.key -out ca.crt -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
SAN="DNS:localhost,IP:127.0.0.1,IP:$NODE_IP,IP:$LAN_IP"
for h in anchor-ship-cse anchor-ship-app anchor-edge-cse anchor-edge-app anchor-edge-b-cse anchor-edge-b-app; do
  SAN="$SAN,DNS:$h"
done
openssl req -newkey rsa:2048 -nodes -subj "/CN=anchor-mec-sandbox" -keyout server.key -out server.csr
printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\n' "$SAN" > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -extfile server.ext -out server.crt
rm -f server.csr server.ext
chmod 600 ca.key server.key
openssl x509 -in server.crt -noout -subject -ext subjectAltName

#!/bin/bash

set -e
set +x

# Get full path to script directory
SCRIPT=$(readlink -f "$0")
BASEDIR=$(dirname "$SCRIPT")

BIN=$BASEDIR/bin/anchor-fleet

echo ""
echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
echo ">>> Dockerizing anchor-fleet MEC app"
echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
echo ""

# Build the Go binary (with the local Go toolchain if present, else in a golang container)
if command -v go >/dev/null; then
    "$BASEDIR/build-anchor-fleet.sh"
else
    docker run --rm -v"$BASEDIR":/opt/local/anchor-fleet golang \
        bash -c "cd /opt/local/anchor-fleet && ./build-anchor-fleet.sh && chown -R $UID:$UID ./bin"
fi

echo ""
echo ">>> Building and pushing Docker image"
docker build --no-cache --rm -t meep-docker-registry:30001/anchor-fleet "$BASEDIR"
docker push meep-docker-registry:30001/anchor-fleet

echo ""
echo ">>> Done"

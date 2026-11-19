#!/bin/bash

set -e
set -x

# Get full path to script directory
SCRIPT=$(readlink -f "$0")
BASEDIR=$(dirname "$SCRIPT")

BIN=$BASEDIR/bin/anchor-fleet/

echo ""
echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
echo ">>> Building anchor-fleet MEC app"
echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
echo ""

cd "$BASEDIR"
go mod tidy
CGO_ENABLED=0 go build -buildvcs=false -o "$BIN" .

echo ""
echo ">>> anchor-fleet build completed"

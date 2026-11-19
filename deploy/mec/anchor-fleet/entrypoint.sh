#!/bin/bash
set -e

# Starts anchor-fleet in a scenario pod. meep-webhook injects MEEP_POD_NAME,
# MEEP_APP_ID, MEEP_SANDBOX_NAME and MEEP_SCENARIO_NAME; everything else
# defaults from MEP_NAME (the MEC host the app is placed on, default mep1):
#   CSE      http://<mep>-meep-acme-mn-cse/acme-cse-mn-<mep>-<sandbox>
#   MEC013   http://<mep>-meep-loc-serv/location/v3
#   MEC011   http://<mep>-meep-app-enablement/mec_app_support/v2
# Any value set in the scenario "Environment variables" box wins.

echo "MEEP_SANDBOX_NAME: ${MEEP_SANDBOX_NAME}"
echo "MEEP_SCENARIO_NAME: ${MEEP_SCENARIO_NAME}"
echo "MEEP_POD_NAME: ${MEEP_POD_NAME}"
echo "MEEP_APP_ID: ${MEEP_APP_ID}"
echo "MEP_NAME: ${MEP_NAME:-mep1}"

exec /anchor-fleet

#!/bin/bash
set -e

LOCATION="swedencentral"
TEMPLATE_FILE="infra/main.bicep"

# Initialize variables
CLIENT_ID=""
PROXY_SECRET=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -c|--client-id) CLIENT_ID="$2"; shift ;;
        -s|--proxy-secret) PROXY_SECRET="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$CLIENT_ID" ]; then
    echo "Error: --client-id argument is required."
    echo "Usage: ./deploy_infra.sh --client-id <ID> [--proxy-secret <SECRET>]"
    exit 1
fi

# NB: the Bicep appSettings array is authoritative, so omitting --proxy-secret
# sets the Function App's PROXY_SHARED_SECRET to empty, disabling the proxy gate
# until the next CI deploy (which injects it from the GitHub environment secret).
if [ -z "$PROXY_SECRET" ]; then
    echo "Warning: --proxy-secret not provided; the Web App -> Function proxy gate will be disabled until the next CI deploy."
fi

echo "Deploying infrastructure to subscription scope in $LOCATION..."
az deployment sub create \
  --location "$LOCATION" \
  --template-file "$TEMPLATE_FILE" \
  --name "deploy-fs-scoremodifier-$(date +%s)" \
  --parameters authClientId="$CLIENT_ID" proxySharedSecret="$PROXY_SECRET"

echo "Infrastructure deployment complete."

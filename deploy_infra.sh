#!/bin/bash
set -e

# Deploys the backend-only infrastructure (storage + Function App + RBAC).
# The frontend, Easy Auth, DNS and custom domain now live in the
# figureskatingtools-site repo — see PROXY-CONTRACT.md.

LOCATION="swedencentral"
TEMPLATE_FILE="infra/main.bicep"

PROXY_SECRET=""
RESOURCE_GROUP=""
PLATFORM_STORAGE=""
ORPHAN_PURGE=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -s|--proxy-secret) PROXY_SECRET="$2"; shift ;;
        -g|--resource-group) RESOURCE_GROUP="$2"; shift ;;
        -p|--platform-storage) PLATFORM_STORAGE="$2"; shift ;;
        --orphan-purge)
            ORPHAN_PURGE="$2"
            if [ "$ORPHAN_PURGE" != "true" ] && [ "$ORPHAN_PURGE" != "false" ]; then
                echo "Error: --orphan-purge must be 'true' or 'false' (got '$ORPHAN_PURGE')."
                exit 1
            fi
            shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$RESOURCE_GROUP" ]; then
    echo "Error: --resource-group argument is required."
    echo "Usage: ./deploy_infra.sh --resource-group <NAME> [--proxy-secret <SECRET>] [--platform-storage <ACCOUNT>] [--orphan-purge true|false]"
    exit 1
fi

# NB: the Bicep appSettings array is authoritative, so omitting --proxy-secret
# sets the Function App's PROXY_SHARED_SECRET to empty, disabling the proxy gate
# until the next CI deploy (which injects it from the GitHub environment secret).
if [ -z "$PROXY_SECRET" ]; then
    echo "Warning: --proxy-secret not provided; the router -> Function proxy gate will be disabled until the next CI deploy."
fi

# Orphan purge (Bicep orphanPurgeEnabled -> ORPHAN_PURGE_ENABLED). The Bicep
# default is true, so a manual run must never fall back to it: without
# --orphan-purge, keep what the Function App has now (the workflow deploys the
# GitHub variable ORPHAN_PURGE_ENABLED), and if that can't be read (no app yet,
# no permission) deploy false (dry run) rather than silently enabling deletion.
if [ -z "$ORPHAN_PURGE" ]; then
    CURRENT_PURGE=""
    FUNCTION_APP=$(az functionapp list --resource-group "$RESOURCE_GROUP" \
        --query "[?starts_with(name, 'func-fs-scoremodifier-')].name" -o tsv 2>/dev/null || true)
    if [ -n "$FUNCTION_APP" ] && [ "$(printf '%s\n' "$FUNCTION_APP" | wc -l)" -eq 1 ]; then
        CURRENT_PURGE=$(az functionapp config appsettings list --resource-group "$RESOURCE_GROUP" \
            --name "$FUNCTION_APP" --query "[?name=='ORPHAN_PURGE_ENABLED'].value | [0]" -o tsv 2>/dev/null \
            | tr -d '[:space:]' || true)
    fi
    if [ "$CURRENT_PURGE" = "true" ] || [ "$CURRENT_PURGE" = "false" ]; then
        ORPHAN_PURGE="$CURRENT_PURGE"
        echo "Keeping the deployed ORPHAN_PURGE_ENABLED=$ORPHAN_PURGE of $FUNCTION_APP (override with --orphan-purge true|false)."
    else
        ORPHAN_PURGE="false"
        echo "Notice: could not read the deployed ORPHAN_PURGE_ENABLED in $RESOURCE_GROUP; deploying it as false (dry run: orphans are only logged). Pass --orphan-purge true to enable deletion."
    fi
fi

echo "Deploying infrastructure to subscription scope in $LOCATION..."
az deployment sub create \
  --location "$LOCATION" \
  --template-file "$TEMPLATE_FILE" \
  --name "deploy-fs-scoremodifier-$(date +%s)" \
  --parameters resourceGroupName="$RESOURCE_GROUP" proxySharedSecret="$PROXY_SECRET" \
    platformStorageAccountName="$PLATFORM_STORAGE" orphanPurgeEnabled="$ORPHAN_PURGE"

echo "Infrastructure deployment complete."

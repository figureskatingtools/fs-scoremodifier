#!/bin/bash
set -e

# 1. Setup Environment (Node 22 LTS)
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    . "$NVM_DIR/nvm.sh" || true
fi
if command -v nvm &> /dev/null; then
    nvm use 22 || nvm install 22
fi
if ! command -v node &> /dev/null; then
    echo "Error: Node.js is not installed or not in PATH."
    exit 1
fi

# 2. Variables
RESOURCE_GROUP=""
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -g|--resource-group) RESOURCE_GROUP="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$RESOURCE_GROUP" ]; then
    echo "Error: --resource-group (-g) is required."
    echo "Usage: ./deploy_frontend.sh --resource-group <resource-group-name>"
    exit 1
fi

echo "Fetching Web App info (RG: $RESOURCE_GROUP)..."
APPS_OUTPUT=$(az webapp list --resource-group "$RESOURCE_GROUP" --query "[?contains(name, 'app-fs-scoremodifier')].{name:name, rg:resourceGroup}" -o tsv)

if [ -z "$APPS_OUTPUT" ]; then
    echo "Error: Could not find any Web App matching 'app-fs-scoremodifier'. Make sure you are logged into Azure CLI."
    exit 1
fi

mapfile -t APP_LINES <<< "$APPS_OUTPUT"
COUNT=${#APP_LINES[@]}

if [ "$COUNT" -eq 1 ]; then
    read -r WEBAPP_NAME SELECTED_RG <<< "${APP_LINES[0]}"
else
    echo "Found multiple Web Apps:"
    for i in "${!APP_LINES[@]}"; do
        read -r NAME RG <<< "${APP_LINES[$i]}"
        echo "$((i+1)). $NAME (RG: $RG)"
    done
    echo -n "Please select the number of the app to deploy to: "
    read -r SELECTION
    if ! [[ "$SELECTION" =~ ^[0-9]+$ ]] || [ "$SELECTION" -lt 1 ] || [ "$SELECTION" -gt "$COUNT" ]; then
        echo "Error: Invalid selection."
        exit 1
    fi
    read -r WEBAPP_NAME SELECTED_RG <<< "${APP_LINES[$((SELECTION-1))]}"
fi

RESOURCE_GROUP="$SELECTED_RG"
echo "Selected Web App: $WEBAPP_NAME (RG: $RESOURCE_GROUP)"

# Fetch Function App URL
echo "Fetching Function App URL..."
FUNC_INFO=$(az functionapp list --resource-group "$RESOURCE_GROUP" --query "[?contains(name, 'func-fs-scoremodifier')].{name:name, host:defaultHostName}" -o tsv | head -n 1)

if [ -z "$FUNC_INFO" ]; then
    echo "Error: Could not find Function App in resource group $RESOURCE_GROUP"
    exit 1
fi

read -r FUNC_APP_NAME FUNC_HOST <<< "$FUNC_INFO"

if [ -z "$FUNC_HOST" ]; then
    echo "Error: Retrieved Function App name ($FUNC_APP_NAME) but hostname is empty."
    exit 1
fi

FUNCTION_APP_URL="https://$FUNC_HOST"
echo "Using Function App URL: $FUNCTION_APP_URL"

# 3. Build Frontend
echo "Building Frontend..."
cd frontend
npm install
npm run build

# 4. Prepare Deployment Artifact (server.js + public/ Vite output)
echo "Creating deployment package..."
STAGE_DIR=$(mktemp -d)
cp server.js "$STAGE_DIR/"
mkdir -p "$STAGE_DIR/public"
cp -r dist/* "$STAGE_DIR/public/"

cd "$STAGE_DIR"
zip -r "$OLDPWD/deploy.zip" .
cd "$OLDPWD"
rm -rf "$STAGE_DIR"

# 5. Configure Web App
echo "Configuring Web App settings..."
az webapp config appsettings set \
  --resource-group "$RESOURCE_GROUP" \
  --name "$WEBAPP_NAME" \
  --settings FUNCTION_APP_URL="$FUNCTION_APP_URL" \
  --output none

az webapp config set \
  --resource-group "$RESOURCE_GROUP" \
  --name "$WEBAPP_NAME" \
  --startup-file "node server.js" \
  --linux-fx-version "NODE|22-lts" \
  --output none

# Disable token store so /.auth/me does not expose tokens
echo "Disabling token store..."
az rest --method PUT \
  --uri "https://management.azure.com/subscriptions/{subscriptionId}/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Web/sites/$WEBAPP_NAME/config/authsettingsV2?api-version=2022-09-01" \
  --body "$(az rest --method GET --uri "https://management.azure.com/subscriptions/{subscriptionId}/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Web/sites/$WEBAPP_NAME/config/authsettingsV2?api-version=2022-09-01" | python3 -c "
import sys, json
config = json.load(sys.stdin)
config.get('properties', {}).setdefault('login', {}).setdefault('tokenStore', {})['enabled'] = False
json.dump(config, sys.stdout)
")" --output none 2>/dev/null || echo "Warning: Could not disable token store via REST API. You may need to do this manually."

# 6. Deploy
echo "Deploying to Azure Web App..."
az webapp deployment source config-zip \
  --resource-group "$RESOURCE_GROUP" \
  --name "$WEBAPP_NAME" \
  --src deploy.zip

echo "Deployment Complete!"
echo "Visit your site at: https://$(az webapp show --name "$WEBAPP_NAME" --resource-group "$RESOURCE_GROUP" --query "defaultHostName" -o tsv)"

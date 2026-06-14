#!/bin/bash

# Usage: ./create_auth_app.sh <AppName> <WebApp_Hostname>
# Example: ./create_auth_app.sh "ScoreModifierApp" "scoremodifier.figureskatingtools.com"

# Exit on error
set -e

APP_NAME=$1
HOSTNAME_ARG=$2

# Validate inputs
if [ -z "$APP_NAME" ] || [ -z "$HOSTNAME_ARG" ]; then
    echo "Error: Missing arguments."
    echo "Usage: $0 <AppName> <WebApp_Hostname>"
    echo "Example: $0 \"ScoreModifierApp\" \"scoremodifier.figureskatingtools.com\""
    exit 1
fi

if [[ "$HOSTNAME_ARG" != http* ]]; then
    REDIRECT_URI="https://$HOSTNAME_ARG/.auth/login/aad/callback"
else
    REDIRECT_URI="$HOSTNAME_ARG/.auth/login/aad/callback"
fi

echo "----------------------------------------------------------------"
echo "Creating Azure App Registration for App Service Easy Auth"
echo "App Name     : $APP_NAME"
echo "Redirect URI : $REDIRECT_URI"
echo "----------------------------------------------------------------"

# 1. Create App Registration (ID tokens enabled, redirect URI, broad audience)
echo "Creating application..."
APP_ID=$(az ad app create \
    --display-name "$APP_NAME" \
    --web-redirect-uris "$REDIRECT_URI" \
    --enable-id-token-issuance true \
    --sign-in-audience AzureADandPersonalMicrosoftAccount \
    --query appId -o tsv)

echo "App created with Client ID: $APP_ID"

echo "Waiting 30 seconds for AzureAD propagation..."
sleep 30

# 2. Set Access Token Version to 2
echo "Configuring requestedAccessTokenVersion to 2..."
for i in {1..5}; do
    OBJECT_ID=$(az ad app show --id "$APP_ID" --query id -o tsv 2>/dev/null) && break
    echo "Retry $i: Waiting for App to populate..."
    sleep 5
done

if [ -z "$OBJECT_ID" ]; then
    echo "Error: Could not retrieve Object ID for App $APP_ID. Azure propagation timeout."
    exit 1
fi

az rest --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
    --headers 'Content-Type=application/json' \
    --body '{"api":{"requestedAccessTokenVersion":2}}'
echo "Access Token Version updated"

# 3. Add User.Read Permission
echo "Adding Microsoft Graph User.Read permission..."
az ad app update --id "$APP_ID" --required-resource-accesses '[{
    "resourceAppId": "00000003-0000-0000-c000-000000000000",
    "resourceAccess": [
        {
            "id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d",
            "type": "Scope"
        }
    ]
}]'
echo "User.Read permission added"

# 4. Create Service Principal (Enterprise Application)
echo "Creating Service Principal (Enterprise Application)..."
SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null || echo "")
if [ -z "$SP_ID" ]; then
    az ad sp create --id "$APP_ID"
    echo "Service Principal created"
else
    echo "Service Principal already exists"
fi

# 5. The federated identity credential linking this app registration to the
#    user-assigned managed identity is created automatically by the CI/CD
#    pipeline after the managed identity is provisioned via Bicep. No client
#    secret is needed.

# 6. Get Tenant ID
TENANT_ID=$(az account show --query tenantId -o tsv)

echo ""
echo "====================================================="
echo "SETUP COMPLETE"
echo "====================================================="
echo "Client ID:  $APP_ID"
echo "Object ID:  $OBJECT_ID"
echo "Tenant ID:  $TENANT_ID"
echo ""
echo "Easy Auth uses federated identity credentials (FIC) with a"
echo "user-assigned managed identity instead of a client secret."
echo ""
echo "NEXT STEPS:"
echo "1. Deploy infrastructure with Bicep (creates the managed identity)"
echo "2. The CI/CD pipeline creates the federated identity credential"
echo "   linking this app registration to the managed identity"
echo "3. Set AUTH_CLIENT_ID=$APP_ID and AUTH_APP_OBJECT_ID=$OBJECT_ID"
echo "   in your GitHub Environment secrets (test and prod)"
echo "4. Azure Portal > Enterprise Applications > $APP_NAME > Permissions"
echo "   -> 'Grant admin consent for <TenantName>'"
echo "5. (Optional) Properties -> set 'Assignment required' to 'Yes'"
echo "====================================================="

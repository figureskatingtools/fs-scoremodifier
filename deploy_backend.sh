#!/bin/bash
set -e

# Parse arguments first
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
    echo "Usage: ./deploy_backend.sh --resource-group <resource-group-name>"
    exit 1
fi

# Repo root (this script lives there)
ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# 1. Prepare Deployment Artifact
BUILD_DIR="$ROOT/backend_build"
echo "Preparing build in $BUILD_DIR..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cp -r "$ROOT/infra/functions/." "$BUILD_DIR/"
# Bundle the canonical core package (repo-root scoremodifier/) so the function
# can `import scoremodifier.per_skater`. Single source of truth, no duplication.
cp -r "$ROOT/scoremodifier" "$BUILD_DIR/scoremodifier"

cd "$BUILD_DIR"

# Cleanup - remove dev/test files and artifacts
rm -rf .venv __pycache__ .git .vscode *.pyc local.settings.json
rm -f test_*.py
find . -type d -name __pycache__ -prune -exec rm -rf {} +

# Install dependencies specifically for Flex Consumption
echo "Installing dependencies to .python_packages..."
mkdir -p .python_packages/lib/site-packages
pip install -r requirements.txt --target .python_packages/lib/site-packages

# Create zip file
echo "Creating backend.zip..."
zip -r "$ROOT/infra/backend.zip" .

cd "$ROOT"

# 2. Get Function App Name
echo "Fetching Function App info (RG: $RESOURCE_GROUP)..."
FUNC_INFO=$(az functionapp list --resource-group "$RESOURCE_GROUP" --query "[?contains(name, 'func-fs-scoremodifier')].{name:name, rg:resourceGroup}" -o tsv | head -n 1)

if [ -z "$FUNC_INFO" ]; then
    echo "Error: Could not find Function App matching 'func-fs-scoremodifier'. Try specifying --resource-group."
    exit 1
fi

read -r FUNC_APP_NAME RESOURCE_GROUP <<< "$FUNC_INFO"

echo "Deploying to Function App: $FUNC_APP_NAME (RG: $RESOURCE_GROUP)"

# 3. Deploy
echo "Publishing function code..."
az functionapp deployment source config-zip -g "$RESOURCE_GROUP" -n "$FUNC_APP_NAME" --src infra/backend.zip

echo "Backend deployment complete."

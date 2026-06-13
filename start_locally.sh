#!/bin/bash

# Function to clean up background processes on exit
cleanup() {
    echo "Stopping services..."
    kill $(jobs -p) 2>/dev/null
}
trap cleanup EXIT

# Load NVM and use Node 22 (LTS)
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

echo "Switching to Node.js 22 (LTS)..."
nvm install 22
nvm use 22

# Ensure Azure Functions Core Tools is installed
if ! command -v func &> /dev/null; then
    echo "Azure Functions Core Tools not found. Installing..."
    npm install -g azure-functions-core-tools@4
fi

echo "Func version: $(func --version)"

# Get script directory for absolute paths
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

echo "Starting Azure Functions Backend..."
cd "$SCRIPT_DIR/infra/functions"
# The function imports the canonical core package from the repo root; expose it.
export PYTHONPATH="$SCRIPT_DIR:$PYTHONPATH"
# Check if .venv exists and activate
if [ -d ".venv" ]; then
    source .venv/bin/activate
    echo "Installing backend dependencies..."
    pip install -r requirements.txt
fi
func start > ../../backend.log 2>&1 &
BACKEND_PID=$!
echo "Backend started (PID: $BACKEND_PID). Logs in backend.log"

echo "Waiting for Backend to initialize..."
sleep 5

echo "Starting Frontend..."
cd "$SCRIPT_DIR/frontend"

# Clean install to fix Vite issues
echo "Cleaning frontend dependencies..."
rm -rf node_modules package-lock.json
npm cache clean --force
npm install
npm rebuild esbuild

npm run dev > ../frontend.log 2>&1 &
FRONTEND_PID=$!
echo "Frontend started (PID: $FRONTEND_PID). Logs in frontend.log"

echo "Waiting for Frontend to initialize..."
sleep 5

echo "Starting SWA Emulator..."
npx swa start http://localhost:5173 --api-location http://localhost:7071

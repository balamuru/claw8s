import sys
import os
import uvicorn

# Add the current directory to sys.path to allow 'dashboard' import
sys.path.append(os.getcwd())

from dashboard.api import app

def main():
    port = int(os.getenv("PORT", 9090))
    print(f"🚀 Claw8s Dashboard starting on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()

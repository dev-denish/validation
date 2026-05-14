"""
AWD Validation API Server
Usage: python scripts/run_server.py
"""
import sys
sys.path.insert(0, "/app/src")

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "awd_validation.api.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="warning",
    )

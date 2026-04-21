import os
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from audit import AuditLog

app = FastAPI(title="Claw8s Audit Dashboard")

# Initialize AuditLog (re-using the same DB)
# In a real app, we'd pass this via dependency injection
DB_PATH = os.getenv("AUDIT_DB_PATH", "kubeclaw_audit.db")
audit = AuditLog(DB_PATH)

@app.on_event("startup")
async def startup():
    await audit.connect()

@app.on_event("shutdown")
async def shutdown():
    await audit.close()

@app.get("/api/incidents")
async def get_incidents():
    return await audit.get_dashboard_data(limit=100)

@app.get("/api/incidents/{incident_id}/actions")
async def get_actions(incident_id: str):
    return await audit.get_incident_actions(incident_id)

@app.get("/api/stats/frequency")
async def get_frequency(minutes: int = 60):
    return await audit.get_incident_frequency(minutes)

@app.delete("/api/incidents")
async def clear_incidents():
    await audit.clear_all_records()
    return {"status": "ok"}

# Serve static files
app.mount("/static", StaticFiles(directory="dashboard/static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse("dashboard/static/index.html")

from fastapi import FastAPI
import uvicorn

app = FastAPI(title="unison-orchestrator")

@app.get("/health")
def health():
    return {"status": "ok", "service": "unison-orchestrator"}

@app.get("/ready")
def ready():
    # Future: check context + storage connectivity
    return {"ready": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

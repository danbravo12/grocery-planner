from fastapi import FastAPI

app = FastAPI(title="Grocery Planner")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

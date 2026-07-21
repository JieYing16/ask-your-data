import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.database import list_tables
from app.llm import ask_question

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Ask Your Data", description="Natural-language SQL over the Chinook database")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/schema")
def schema() -> dict:
    return {"tables": list_tables()}


@app.post("/api/ask")
def ask(request: AskRequest) -> dict:
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty.")
    try:
        return ask_question(question)
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the client
        raise HTTPException(status_code=502, detail=f"Failed to answer question: {exc}") from exc


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

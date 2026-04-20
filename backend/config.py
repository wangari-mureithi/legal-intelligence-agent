"""
Centralised configuration and shared singletons.

Keeps LLM and checkpointer initialisation in one place so every node
imports from here rather than duplicating .env reads.
"""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level above backend/)
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

# ── LLM ───────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_llm():
    """Return a cached ChatGroq instance."""
    from langchain_groq import ChatGroq

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GROQ_API_KEY is not set. "
            "Add it to your .env file or set it as an environment variable."
        )
    return ChatGroq(
        model="llama-3.3-70b-versatile",
        temperature=0.1,
        max_tokens=8000,     # llama-3.3-70b supports up to 8192 output tokens
        groq_api_key=api_key,
    )


# ── LangGraph checkpointer ────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_checkpointer():
    """Return a SqliteSaver for LangGraph graph state persistence."""
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = _ROOT / "data" / "checkpoints.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)


# ── Path helpers ──────────────────────────────────────────────────────────────

def sources_path() -> Path:
    return _ROOT / "config" / "sources.json"


def clients_path() -> Path:
    return _ROOT / "config" / "clients.json"


def data_path() -> Path:
    return _ROOT / "data"

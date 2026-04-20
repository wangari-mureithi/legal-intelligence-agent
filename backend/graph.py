"""
LangGraph agent definition.

Graph topology:
  web_scraper → relevance_filter → [summarizer | END]
                                          ↓
                                    human_review → [email_dispatcher | END]
                                                          ↓
                                                      audit_log → END

The human_review node calls interrupt() which pauses the graph.
The Streamlit UI resumes the graph using:

    from langgraph.types import Command
    graph.invoke(
        Command(resume={"approval_status": "approved",
                        "reviewer_notes": "...",
                        "updated_draft": {...},
                        "reviewed_by": "partner@firm.com"}),
        config={"configurable": {"thread_id": thread_id}}
    )
"""

import logging
from typing import Literal

from langgraph.graph import StateGraph, END

from backend.state import AlertState
from backend.nodes.web_scraper import web_scraper_node
from backend.nodes.relevance_filter import relevance_filter_node
from backend.nodes.summarizer import summarizer_node
from backend.nodes.human_review import human_review_node
from backend.nodes.email_dispatcher import email_dispatcher_node
from backend.config import get_checkpointer
from backend import database as db

logger = logging.getLogger(__name__)


# ── Audit log node ────────────────────────────────────────────────────────────

def audit_log_node(state: AlertState) -> dict:
    """Writes the final dispatch result to the audit log and returns unchanged state."""
    alert_id = state.get("thread_id", "")
    dispatched = state.get("dispatched_email", False)
    db.log_action(
        alert_id=alert_id,
        action="workflow_complete",
        details={
            "dispatched_email": dispatched,
            "dispatch_error": state.get("dispatch_error", ""),
            "approval_status": state.get("approval_status", ""),
        },
    )
    return {}


# ── Routing functions ─────────────────────────────────────────────────────────

def route_after_relevance(state: AlertState) -> Literal["summarizer", "__end__"]:
    if state.get("is_relevant"):
        return "summarizer"
    logger.info(
        "Content from %s deemed not relevant — ending graph.",
        state.get("source_name"),
    )
    return END


def route_after_review(state: AlertState) -> Literal["email_dispatcher", "__end__"]:
    status = state.get("approval_status", "")
    if status == "approved":
        return "email_dispatcher"
    logger.info(
        "Alert %s %s — ending graph without dispatch.",
        state.get("thread_id"),
        status,
    )
    return END


# ── Graph assembly ────────────────────────────────────────────────────────────

def build_graph():
    """Compile and return the LangGraph StateGraph with checkpointer."""
    builder = StateGraph(AlertState)

    # Register nodes
    builder.add_node("web_scraper", web_scraper_node)
    builder.add_node("relevance_filter", relevance_filter_node)
    builder.add_node("summarizer", summarizer_node)
    builder.add_node("human_review", human_review_node)
    builder.add_node("email_dispatcher", email_dispatcher_node)
    builder.add_node("audit_log", audit_log_node)

    # Entry point
    builder.set_entry_point("web_scraper")

    # Edges
    builder.add_edge("web_scraper", "relevance_filter")
    builder.add_conditional_edges(
        "relevance_filter",
        route_after_relevance,
        {"summarizer": "summarizer", END: END},
    )
    builder.add_edge("summarizer", "human_review")
    builder.add_conditional_edges(
        "human_review",
        route_after_review,
        {"email_dispatcher": "email_dispatcher", END: END},
    )
    builder.add_edge("email_dispatcher", "audit_log")
    builder.add_edge("audit_log", END)

    checkpointer = get_checkpointer()
    return builder.compile(checkpointer=checkpointer)


# ── Module-level singleton ────────────────────────────────────────────────────
# Import this wherever you need to invoke or inspect the graph.

graph = build_graph()

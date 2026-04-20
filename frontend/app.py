"""
Legal Intelligence Agent — Streamlit Application Entry Point.

Run with:
    streamlit run frontend/app.py

Navigation is handled via st.sidebar.radio. Each page is a module
in frontend/pages/ that exposes a render() function.
"""

import sys
from pathlib import Path

# Ensure the project root is on sys.path so backend imports work from the
# frontend/ directory.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st
from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

# Bootstrap the database on first run
from backend.database import init_db
init_db()

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Legal Intelligence Agent",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────

st.markdown("""
<style>
  /* Sidebar branding */
  [data-testid="stSidebar"] { background-color: #1a1a2e; }
  [data-testid="stSidebar"] * { color: #e8e8e8 !important; }
  [data-testid="stSidebar"] .stRadio label { font-size: 15px; padding: 4px 0; }

  /* Status badges */
  .badge-pending  { background:#e6a817; color:#fff; padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600; }
  .badge-approved { background:#28a745; color:#fff; padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600; }
  .badge-rejected { background:#dc3545; color:#fff; padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600; }

  /* Alert card */
  .alert-card { border:1px solid #ddd; border-radius:8px; padding:20px; margin-bottom:16px; }
  .alert-headline { font-size:17px; font-weight:700; color:#1a1a2e; }
  .section-label { color:#c41e3a; font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:1px; margin-top:18px; margin-bottom:4px; }

  /* Metric cards */
  div[data-testid="metric-container"] { background:#f8f9fa; border-radius:8px; padding:12px; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar navigation ────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## ⚖️ Legal Intelligence")
    st.markdown("---")
    page = st.radio(
        "Navigate",
        options=["Dashboard", "Alert Review", "Settings", "Audit Log"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    st.caption("Powered by LangGraph + Groq")

# ── Page routing ──────────────────────────────────────────────────────────────

if page == "Dashboard":
    from frontend.views.dashboard import render
    render()
elif page == "Alert Review":
    from frontend.views.alert_review import render
    render()
elif page == "Settings":
    from frontend.views.settings import render
    render()
elif page == "Audit Log":
    from frontend.views.audit_log import render
    render()

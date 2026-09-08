"""Optional interactive cockpit (Streamlit). Install extras: pip install -e ".[cockpit]"
Run: streamlit run pqm_agent/cockpit_app.py -- --db out/demo/pqm_agent.db
The Power BI cockpit consumes the same CSV exports produced by `pqm-agent cockpit`."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path


def main() -> None:
    import pandas as pd
    import streamlit as st

    from pqm_agent.cockpit import CockpitService
    from pqm_agent.config import load_settings
    from pqm_agent.store import Store

    db = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--db=")), None)
    if db is None and "--db" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1]
    db = db or "out/demo/pqm_agent.db"
    st.set_page_config(page_title="8D / PQM Cockpit", layout="wide")
    st.title("8D / PQM Quality-Escalation Cockpit")
    st.caption("P3 Rank 6 - every official decision needs a named human approval; the agent proposes and monitors.")
    svc = CockpitService(Store(db), load_settings())
    today = st.sidebar.date_input("As of", value=date.today())
    rows = svc.case_control_rows(today)
    kpis = svc.executive_kpis(rows, today)
    quality = svc.agent_quality()
    cols = st.columns(5)
    cols[0].metric("Open cases", kpis["open_cases"])
    cols[1].metric("8D on-time", f"{kpis['on_time_closure_rate']:.0%}" if kpis["on_time_closure_rate"] is not None else "n/a",
                   help=f"target {kpis['on_time_target']:.0%}, baseline {kpis['on_time_baseline']:.0%}")
    cols[2].metric("Cases at risk", kpis["cases_at_risk"])
    cols[3].metric("Root cause not confirmed", kpis["root_cause_not_confirmed"])
    cols[4].metric("PFMEA / CP confirmation missing", kpis["pfmea_cp_confirmation_missing"])
    st.subheader("Case control")
    df = pd.DataFrame(rows)
    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True)
    st.subheader("Agent quality")
    st.json(quality)
    if st.button("Export CSV / HTML for Power BI"):
        paths = svc.export(Path("out/cockpit"), today)
        st.success("Exported: " + ", ".join(str(p) for p in paths.values()))


if __name__ == "__main__":
    main()

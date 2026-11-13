"""Streamlit front end for the IPO tracker. Reuses the data code in server.py."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import server


def today():
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")


def status_of(i, t):
    if i.get("list") and t >= i["list"]:
        return "Listed"
    if i.get("close") and t > i["close"]:
        return "Closed"
    if i.get("open") and t >= i["open"]:
        return "Open"
    return "Upcoming"


def nice(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")
    except (TypeError, ValueError):
        return "TBA"


def to_rows(items, t):
    rows = []
    for i in items:
        hi, lo, gmp, lot = i.get("hi") or 0, i.get("lo") or 0, i.get("gmp") or 0, i.get("lot") or 0
        band = "-" if not hi else (f"{hi:g}" if lo == hi or not lo else f"{lo:g} - {hi:g}")
        rows.append({
            "Company": i["name"],
            "Status": status_of(i, t),
            "Price band (Rs)": band,
            "GMP %": round(gmp / hi * 100, 1) if hi else 0.0,
            "Opens": nice(i.get("open")),
            "Closes": nice(i.get("close")),
            "Listing": nice(i.get("list")),
            "Est. listing (Rs)": round(hi + gmp, 2) if hi else None,
            "Actual listing (Rs)": i.get("actual"),
            "Listing gain %": round((i["actual"] - hi) / hi * 100, 1) if i.get("actual") and hi else None,
            "Subscribed total (x)": i.get("sub_total"),
            "Retail (x)": i.get("sub_retail"),
            "HNI/NII (x)": i.get("sub_hni"),
            "QIB (x)": i.get("sub_qib"),
            "Lot size": lot or None,
            "Min. investment (Rs)": int(lot * hi) if lot and hi else None,
            "_open": i.get("open") or "9999", "_list": i.get("list") or "9999",
        })
    return rows


@st.cache_data(ttl=600, show_spinner="Loading IPO data...")
def load():
    data, _ = server.build()
    return data


st.set_page_config(page_title="IPO Tracker", layout="wide")
st.title("IPO Tracker")
st.caption("Price band, grey market premium (GMP), apply and listing dates. "
           "GMP is unofficial and changes daily. Verify everything with your broker before applying. "
           "This is not investment advice.")

c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
q = c1.text_input("Search company", "")
f = c2.selectbox("Status", ["All", "Upcoming", "Open", "Closed", "Listed"])
s = c3.selectbox("Sort by", ["Apply date", "GMP %", "Listing date"])
c4.write("")
if c4.button("Refresh"):
    st.cache_data.clear()

data = load()
df = pd.DataFrame(to_rows(data["items"], today()))
if df.empty:
    st.info("No IPOs found right now.")
else:
    if q:
        df = df[df["Company"].str.contains(q, case=False, na=False)]
    if f != "All":
        df = df[df["Status"] == f]
    key = {"Apply date": ("_open", True), "GMP %": ("GMP %", False), "Listing date": ("_list", True)}[s]
    df = df.sort_values(key[0], ascending=key[1]).drop(columns=["_open", "_list"])
    st.dataframe(df, use_container_width=True, hide_index=True)

if data.get("error"):
    st.warning("NSE: " + data["error"])
if data.get("gmp_note"):
    st.caption(data["gmp_note"])
if data.get("sub_note"):
    st.caption(data["sub_note"])
with st.expander("Diagnostics (if subscription data is missing)"):
    if st.button("Check subscription source"):
        st.json(server.debug_subs())
st.caption("Updated " + datetime.fromtimestamp(data["updated"], ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST"))

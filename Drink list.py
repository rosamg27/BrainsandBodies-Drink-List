"""
Getränke-Strichliste digital – Stempelpass-System
===================================================

Funktionsweise (kurz):
- Jede:r Kunde/Kundin hat eine eigene ID (uid) -> eigener QR-Code -> eigener Link.
- Kunden-Ansicht (Link/QR am Kühlschrank oder am Handy): zeigt den aktuellen
  Stand als "Stempelpass" (wie viele Getränke noch übrig sind) + Kauf-Buttons
  für 1er/5er/10er Blöcke (Stripe Checkout).
- Mitarbeiter-Ansicht (mit PIN geschützt): Getränk abbuchen (mit Zeitstempel
  und Namen des Mitarbeiters), neue Kunden anlegen + deren QR-Code erzeugen,
  Verlauf einsehen.

Keine neue App nötig: läuft als normale Webseite (Streamlit), der QR-Code
am Kühlschrank ist einfach ein Link auf diese Seite.
"""

import os
import sqlite3
import uuid
from datetime import datetime
from io import BytesIO

import qrcode
import streamlit as st
import stripe

# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------

DB_PATH = os.path.join(os.getcwd(), "getraenke.db")  # Ensure DB_PATH is set correctly

st.set_page_config(page_title="Getränke-Konto", page_icon="🥤", layout="centered")

try:
    stripe.api_key = st.secrets["STRIPE_SECRET_KEY"]
    APP_URL = st.secrets["APP_URL"].rstrip("/")
    STAFF_PIN = str(st.secrets["STAFF_PIN"])
    BLOCKS = {
        "1": {"price_id": st.secrets["PRICE_1"], "credits": 1, "label": "1er Block"},
        "5": {"price_id": st.secrets["PRICE_5"], "credits": 5, "label": "5er Block"},
        "10": {"price_id": st.secrets["PRICE_10"], "credits": 10, "label": "10er Block"},
    }
    SECRETS_OK = True
except Exception:
    SECRETS_OK = False

# ----------------------------------------------------------------------------
# Your other functions (get_conn, customer_view, staff_view, etc.)
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Haupt-Router
# ----------------------------------------------------------------------------

def main():
    conn = get_conn()
    params = st.query_params

    if "session_id" in params and "uid" in params and SECRETS_OK:
        sync_stripe_payment(conn, params["uid"], params["session_id"])

    if "staff" in params:
        staff_view(conn)
    elif "uid" in params:
        # Rücksprung von Stripe nach der Zahlung
        customer_view(conn, params["uid"])
    elif st.session_state.get("selected_uid"):
        if st.button("← Andere Person"):
            st.session_state.selected_uid = None
            st.rerun()
        customer_view(conn, st.session_state.selected_uid)
    else:
        name_selection_view(conn)

if __name__ == "__main__":
    main()
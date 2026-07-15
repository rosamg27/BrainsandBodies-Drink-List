import sqlite3  # For database operations
import uuid  # For generating unique customer IDs
from datetime import datetime  # For timestamping transactions
from io import BytesIO  # For handling QR code image data
import qrcode  # For generating QR codes
import streamlit as st  # For creating the web interface
import stripe  # For Stripe payment integration

# Set the Streamlit page configuration (must be called exactly once, before any other st.* call)
st.set_page_config(page_title="Getränke-Konto", page_icon="🥤", layout="centered")

# Configuration Setup

# Define the database path
DB_PATH = "getraenke.db"

# Initialize configuration variables for Stripe and application settings
try:
    stripe.api_key = st.secrets["STRIPE_SECRET_KEY"]  # Stripe API key
    APP_URL = st.secrets["APP_URL"].rstrip("/")  # Application URL
    STAFF_PIN = str(st.secrets["STAFF_PIN"])  # Staff PIN for protected access
    BLOCKS = {
        "1": {"price_id": st.secrets["PRICE_1"], "credits": 1, "label": "1er Block"},
        "5": {"price_id": st.secrets["PRICE_5"], "credits": 5, "label": "5er Block"},
        "10": {"price_id": st.secrets["PRICE_10"], "credits": 10, "label": "10er Block"},
    }
    SECRETS_OK = True  # Flag to indicate secrets are correctly configured
except Exception:
    SECRETS_OK = False  # Flag to indicate secrets are not configured properly


# Database Initialization

def initialize_database():
    """Create the SQLite tables if they don't exist yet."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            credits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT NOT NULL,
            type TEXT NOT NULL,
            amount INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            employee TEXT,
            stripe_session_id TEXT
        )"""
    )
    conn.commit()
    conn.close()


initialize_database()


def get_customer(conn, uid):
    """Retrieve customer details (id, name, credits) by unique ID, or None."""
    return conn.execute(
        "SELECT id, name, credits FROM customers WHERE id = ?", (uid,)
    ).fetchone()


def create_customer(conn, name):
    """Create a new customer and return their generated ID."""
    uid = uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO customers (id, name, credits, created_at) VALUES (?, ?, 0, ?)",
        (uid, name, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return uid


def log_transaction(conn, customer_id, ttype, amount, employee=None, session_id=None):
    """Insert a row into the transactions log."""
    conn.execute(
        """INSERT INTO transactions (customer_id, type, amount, timestamp, employee, stripe_session_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (customer_id, ttype, amount, datetime.now().isoformat(timespec="seconds"), employee, session_id),
    )
    conn.commit()


def deduct_drink(conn, customer_id, employee_name):
    """Deduct one credit and log the consumption."""
    conn.execute(
        "UPDATE customers SET credits = credits - 1 WHERE id = ? AND credits > 0",
        (customer_id,),
    )
    conn.commit()
    log_transaction(conn, customer_id, "verbrauch", -1, employee=employee_name)


def already_processed(conn, session_id):
    """Check whether a Stripe session was already credited."""
    row = conn.execute(
        "SELECT 1 FROM transactions WHERE stripe_session_id = ?", (session_id,)
    ).fetchone()
    return row is not None


def sync_stripe_payment(conn, uid, session_id):
    """Verify a Stripe checkout session and credit the customer once."""
    if already_processed(conn, session_id):
        return

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        st.error(f"Zahlung konnte nicht überprüft werden: {e}")
        return

    if session.payment_status == "paid" and session.client_reference_id == uid:
        credits = int(session.metadata.get("credits", 0))
        conn.execute(
            "UPDATE customers SET credits = credits + ? WHERE id = ?", (credits, uid)
        )
        conn.commit()
        log_transaction(conn, uid, "kauf", credits, session_id=session_id)
        st.success(f"Zahlung erfolgreich! {credits} Getränke wurden gutgeschrieben.")


def create_checkout_session(uid, block_key):
    """Create a Stripe checkout session and return its URL."""
    block = BLOCKS[block_key]
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": block["price_id"], "quantity": 1}],
        client_reference_id=uid,
        metadata={"credits": block["credits"]},
        success_url=f"{APP_URL}/?uid={uid}&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_URL}/?uid={uid}",
    )
    return session.url


def generate_qr_code(link):
    """Return a PNG QR code (as BytesIO) encoding the given link."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def customer_view(conn, uid):
    """Render the customer-facing interface."""
    customer = get_customer(conn, uid)
    if not customer:
        st.error("Unbekannter Code. Bitte beim Personal melden.")
        return

    _, name, credits = customer
    st.title(f"Hallo, {name}! 👋")

    filled = min(credits, 10)
    empty = max(10 - filled, 0) if credits <= 10 else 0
    if credits > 10:
        st.markdown(f"### 🥤 {credits} Getränke übrig")
    else:
        stamps = "🟢 " * filled + "⚪ " * empty
        st.markdown(f"### {stamps}")
        st.caption(f"{filled} von 10 Getränken übrig")

    if credits <= 0:
        st.warning("Dein Guthaben ist aufgebraucht – bitte neuen Block kaufen.")
    elif credits <= 2:
        st.info("Dein Guthaben ist bald aufgebraucht.")

    st.divider()

    if st.button("🥤 Getränk genommen (−1)", disabled=credits <= 0, type="primary", use_container_width=True):
        deduct_drink(conn, uid, employee_name=None)
        st.success("Verbucht, guten Durst!")
        st.rerun()

    st.caption("Bitte erst klicken, nachdem du dir das Getränk aus dem Kühlschrank genommen hast.")

    st.divider()
    st.subheader("Neuen Block kaufen")

    if not SECRETS_OK:
        st.error("Stripe ist noch nicht konfiguriert (siehe README).")
    else:
        cols = st.columns(3)
        for col, key in zip(cols, ["1", "5", "10"]):
            block = BLOCKS[key]
            with col:
                if st.button(block["label"], key=f"buy_{key}", use_container_width=True):
                    url = create_checkout_session(uid, key)
                    st.markdown(f"[Zur Kasse →]({url})", unsafe_allow_html=True)

    with st.expander("Mein Verlauf"):
        rows = conn.execute(
            """SELECT type, amount, timestamp FROM transactions
               WHERE customer_id = ? ORDER BY timestamp DESC LIMIT 20""",
            (uid,),
        ).fetchall()
        if rows:
            for ttype, amount, ts in rows:
                label = "Kauf" if ttype == "kauf" else "Getränk entnommen"
                st.write(f"{ts} — {label} ({amount:+d})")
        else:
            st.write("Noch keine Einträge.")


def staff_view(conn):
    """Render the staff interface (login, log, new customer/QR, corrections)."""
    st.title("🔑 Mitarbeiter-Bereich")

    if "staff_ok" not in st.session_state:
        st.session_state.staff_ok = False

    if not st.session_state.staff_ok:
        pin = st.text_input("PIN", type="password")
        if st.button("Anmelden"):
            if SECRETS_OK and pin == STAFF_PIN:
                st.session_state.staff_ok = True
                st.rerun()
            else:
                st.error("Falscher PIN.")
        return

    employee_name = st.text_input("Dein Name (für das Protokoll, nur bei Korrekturen nötig)", key="emp_name")

    tab1, tab2, tab3 = st.tabs(["Verlauf (Kontrolle)", "Neuer Kunde + QR-Code", "Korrektur"])

    with tab1:
        st.caption(
            "Kunden buchen ihre Getränke selbst ab – hier könnt ihr den Verlauf "
            "kontrollieren (analog zur alten Strichliste)."
        )
        rows = conn.execute(
            """SELECT t.timestamp, c.name, t.type, t.amount, t.employee
               FROM transactions t JOIN customers c ON c.id = t.customer_id
               ORDER BY t.timestamp DESC LIMIT 100"""
        ).fetchall()
        if rows:
            st.dataframe(
                [{"Zeit": ts, "Kunde": n, "Typ": ty, "Menge": am, "Von": emp or "Kunde selbst"}
                 for ts, n, ty, am, emp in rows],
                use_container_width=True,
            )
        else:
            st.write("Noch keine Buchungen.")

    with tab2:
        new_name = st.text_input("Name des neuen Kunden")
        if st.button("Kunde anlegen"):
            if new_name.strip():
                create_customer(conn, new_name.strip())
                st.success(f"{new_name} wurde angelegt und erscheint jetzt in der Namensliste.")
            else:
                st.error("Bitte einen Namen eingeben.")

        st.divider()
        st.caption(
            "Es gibt nur EINEN QR-Code für alle (am Kühlschrank). Kunden wählen "
            "nach dem Scannen ihren Namen aus einer Liste aus."
        )
        if SECRETS_OK:
            png = generate_qr_code(APP_URL).getvalue()
            st.image(png, caption="QR-Code für den Kühlschrank (für alle gleich)", width=250)
            st.download_button(
                "Kühlschrank-QR-Code herunterladen", data=png,
                file_name="qr_kuehlschrank.png", mime="image/png",
            )
            st.code(APP_URL)
        else:
            st.error("APP_URL ist nicht konfiguriert (siehe Secrets).")

    with tab3:
        st.caption(
            "Nur für Korrekturen (z. B. falsche Selbstbuchung eines Kunden) – "
            "im Normalbetrieb nicht nötig."
        )
        rows = conn.execute("SELECT id, name, credits FROM customers ORDER BY name").fetchall()
        options = {f"{n} ({c} übrig)": cid for cid, n, c in rows}
        if not options:
            st.info("Noch keine Kunden angelegt.")
        else:
            choice = st.selectbox("Kunde auswählen", list(options.keys()))
            uid = options[choice]
            credits = get_customer(conn, uid)[2]
            st.write(f"Aktueller Stand: **{credits}**")
            disabled = credits <= 0 or not employee_name
            if st.button("Getränk manuell abbuchen (−1)", disabled=disabled):
                deduct_drink(conn, uid, employee_name)
                st.success(f"Korrigiert um {datetime.now().strftime('%H:%M:%S')} von {employee_name}.")
                st.rerun()
            if not employee_name:
                st.caption("Bitte oben deinen Namen eintragen.")

    if st.button("Abmelden"):
        st.session_state.staff_ok = False
        st.rerun()


def main_router():
    """Route to staff view, customer view, or the name-selection screen."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    params = st.query_params  # dict-like: values are plain strings, not lists

    if "session_id" in params and "uid" in params and SECRETS_OK:
        sync_stripe_payment(conn, params["uid"], params["session_id"])

    if "staff" in params:
        staff_view(conn)

    elif "uid" in params:
        customer_view(conn, params["uid"])

    elif st.session_state.get("selected_uid"):
        if st.button("← Andere Person"):
            st.session_state.selected_uid = None
            st.rerun()
        customer_view(conn, st.session_state.selected_uid)

    else:
        rows = conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
        if not rows:
            st.info("Es sind noch keine Kunden angelegt. Bitte beim Personal melden.")
            st.caption("Hinweis für Personal: über ?staff in der URL gelangt ihr in den Mitarbeiter-Bereich.")
            return

        names = {n: cid for cid, n in rows}
        choice = st.selectbox(
            "Wer bist du?", options=["– bitte auswählen –"] + list(names.keys())
        )
        if choice != "– bitte auswählen –":
            st.session_state.selected_uid = names[choice]
            st.rerun()


main_router()

import sqlite3  # For database operations
import uuid  # For generating unique customer IDs
from datetime import datetime  # For timestamping transactions
from io import BytesIO  # For handling QR code image data
import qrcode  # For generating QR codes
import streamlit as st  # For creating the web interface
import stripe  # For Stripe payment integration
import pandas as pd  # For table/dataframe display

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
            email TEXT,
            credits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    # Migration: add the email column if this is an older database that
    # was created before email support existed.
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(customers)")]
    if "email" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN email TEXT")
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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fridges (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS drink_types (
            id TEXT PRIMARY KEY,
            fridge_id TEXT NOT NULL,
            name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stock_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drink_type_id TEXT NOT NULL,
            change INTEGER NOT NULL,
            reason TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            employee TEXT
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


def get_customer_by_email(conn, email):
    """Retrieve customer id by email (case-insensitive), or None."""
    row = conn.execute(
        "SELECT id FROM customers WHERE lower(email) = lower(?)", (email.strip(),)
    ).fetchone()
    return row[0] if row else None


def create_customer(conn, name, email):
    """Create a new customer and return their generated ID."""
    uid = uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO customers (id, name, email, credits, created_at) VALUES (?, ?, ?, 0, ?)",
        (uid, name, email, datetime.now().isoformat(timespec="seconds")),
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


def create_fridge(conn, name):
    """Create a new labeled fridge and return its ID."""
    fid = uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO fridges (id, name, created_at) VALUES (?, ?, ?)",
        (fid, name, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return fid


def create_drink_type(conn, fridge_id, name, quantity):
    """Create a new drink type in a fridge with a starting quantity."""
    tid = uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO drink_types (id, fridge_id, name, quantity, created_at) VALUES (?, ?, ?, ?, ?)",
        (tid, fridge_id, name, quantity, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    if quantity:
        log_stock_change(conn, tid, quantity, "startbestand", employee=None)
    return tid


def log_stock_change(conn, drink_type_id, change, reason, employee=None):
    """Adjust a drink type's quantity and record the change for statistics."""
    conn.execute(
        "UPDATE drink_types SET quantity = quantity + ? WHERE id = ?",
        (change, drink_type_id),
    )
    conn.execute(
        """INSERT INTO stock_log (drink_type_id, change, reason, timestamp, employee)
           VALUES (?, ?, ?, ?, ?)""",
        (drink_type_id, change, reason, datetime.now().isoformat(timespec="seconds"), employee),
    )
    conn.commit()


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
        stamps = "🥤 " * filled + "⚪ " * empty
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

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Verlauf (Kontrolle)", "Neuer Kunde + QR-Code", "Korrektur", "Kühlschränke & Sorten"]
    )

    with tab1:
        st.subheader("Kundenübersicht")
        cust_rows = conn.execute(
            "SELECT name, email, credits FROM customers ORDER BY name"
        ).fetchall()
        if cust_rows:
            st.dataframe(
                pd.DataFrame(
                    [{"Name": n, "E-Mail": e or "–", "Guthaben": c} for n, e, c in cust_rows]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.write("Noch keine Kunden angelegt.")

        st.divider()
        st.subheader("Verlauf")
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
        new_email = st.text_input("E-Mail-Adresse des neuen Kunden")
        if st.button("Kunde anlegen"):
            if not new_name.strip():
                st.error("Bitte einen Namen eingeben.")
            elif not new_email.strip() or "@" not in new_email:
                st.error("Bitte eine gültige E-Mail-Adresse eingeben.")
            elif get_customer_by_email(conn, new_email):
                st.error("Diese E-Mail-Adresse ist bereits vergeben.")
            else:
                create_customer(conn, new_name.strip(), new_email.strip())
                st.success(f"{new_name} wurde angelegt und kann sich jetzt mit dieser E-Mail-Adresse anmelden.")

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

    with tab4:
        st.subheader("Kühlschrank anlegen")
        new_fridge_name = st.text_input("Bezeichnung (z. B. 'Kühlschrank Büro EG')", key="new_fridge_name")
        if st.button("Kühlschrank anlegen"):
            if new_fridge_name.strip():
                create_fridge(conn, new_fridge_name.strip())
                st.success(f"Kühlschrank '{new_fridge_name}' wurde angelegt.")
            else:
                st.error("Bitte eine Bezeichnung eingeben.")

        st.divider()

        fridge_rows = conn.execute("SELECT id, name FROM fridges ORDER BY name").fetchall()
        fridge_options = {n: fid for fid, n in fridge_rows}

        st.subheader("Sorte anlegen")
        if not fridge_options:
            st.info("Bitte zuerst oben einen Kühlschrank anlegen.")
        else:
            fridge_choice = st.selectbox("Kühlschrank", list(fridge_options.keys()), key="type_fridge_choice")
            new_type_name = st.text_input("Sorte (z. B. 'Almdudler 0,33l')", key="new_type_name")
            start_qty = st.number_input("Startbestand", min_value=0, step=1, value=0, key="new_type_qty")
            if st.button("Sorte anlegen"):
                if new_type_name.strip():
                    create_drink_type(conn, fridge_options[fridge_choice], new_type_name.strip(), int(start_qty))
                    st.success(f"'{new_type_name}' wurde in '{fridge_choice}' angelegt.")
                else:
                    st.error("Bitte eine Sortenbezeichnung eingeben.")

        st.divider()

        st.subheader("Bestandsübersicht")
        overview_rows = conn.execute(
            """SELECT f.name, dt.name, dt.quantity
               FROM drink_types dt JOIN fridges f ON f.id = dt.fridge_id
               ORDER BY f.name, dt.name"""
        ).fetchall()
        if overview_rows:
            st.dataframe(
                pd.DataFrame(
                    [{"Kühlschrank": f, "Sorte": s, "Bestand": q} for f, s, q in overview_rows]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.write("Noch keine Sorten angelegt.")

        st.divider()

        st.subheader("Bestand anpassen (Einkauf / Entnahme / Korrektur)")
        type_rows = conn.execute(
            """SELECT dt.id, f.name, dt.name, dt.quantity
               FROM drink_types dt JOIN fridges f ON f.id = dt.fridge_id
               ORDER BY f.name, dt.name"""
        ).fetchall()
        type_options = {f"{fn} – {tn} ({q} übrig)": tid for tid, fn, tn, q in type_rows}
        if not type_options:
            st.info("Noch keine Sorten angelegt.")
        else:
            type_choice = st.selectbox("Sorte auswählen", list(type_options.keys()), key="stock_type_choice")
            type_id = type_options[type_choice]
            reason = st.selectbox("Grund", ["einkauf", "entnahme", "korrektur"], key="stock_reason")
            qty_change = st.number_input(
                "Menge (bei 'entnahme' automatisch abgezogen)", min_value=1, step=1, value=1, key="stock_qty"
            )
            if st.button("Bestand aktualisieren"):
                signed_change = -int(qty_change) if reason == "entnahme" else int(qty_change)
                log_stock_change(conn, type_id, signed_change, reason, employee=employee_name or None)
                st.success("Bestand aktualisiert.")
                st.rerun()

        st.divider()

        st.subheader("Einkaufsstatistik")
        st.caption(
            "Zeigt, wie viel von jeder Sorte über die Zeit eingekauft und entnommen wurde – "
            "als Orientierung für den nächsten Einkauf."
        )
        period = st.selectbox(
            "Zeitraum", ["Letzte 30 Tage", "Letzte 90 Tage", "Letzte 365 Tage", "Gesamter Verlauf"],
            key="stats_period",
        )
        days_map = {"Letzte 30 Tage": 30, "Letzte 90 Tage": 90, "Letzte 365 Tage": 365, "Gesamter Verlauf": None}
        days = days_map[period]

        query = """SELECT f.name, dt.name, sl.reason, sl.change
                   FROM stock_log sl
                   JOIN drink_types dt ON dt.id = sl.drink_type_id
                   JOIN fridges f ON f.id = dt.fridge_id"""
        params = []
        if days is not None:
            query += " WHERE sl.timestamp >= datetime('now', ?)"
            params.append(f"-{days} days")
        stat_rows = conn.execute(query, params).fetchall()

        if not stat_rows:
            st.write("Noch keine Bestandsbewegungen im gewählten Zeitraum.")
        else:
            df_stats = pd.DataFrame(stat_rows, columns=["Kühlschrank", "Sorte", "Grund", "Menge"])
            summary = (
                df_stats[df_stats["Grund"] == "einkauf"]
                .groupby("Sorte")["Menge"].sum()
                .sort_values(ascending=False)
                .reset_index()
                .rename(columns={"Menge": "Eingekauft"})
            )
            consumed = (
                df_stats[df_stats["Grund"] == "entnahme"]
                .groupby("Sorte")["Menge"].sum()
                .abs()
                .reset_index()
                .rename(columns={"Menge": "Entnommen"})
            )
            merged = pd.merge(summary, consumed, on="Sorte", how="outer").fillna(0)
            merged["Eingekauft"] = merged["Eingekauft"].astype(int)
            merged["Entnommen"] = merged["Entnommen"].astype(int)
            merged = merged.sort_values("Entnommen", ascending=False)
            st.dataframe(merged, use_container_width=True, hide_index=True)
            st.bar_chart(merged.set_index("Sorte")[["Eingekauft", "Entnommen"]])

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
        rows = conn.execute("SELECT id FROM customers").fetchall()
        if not rows:
            st.info("Es sind noch keine Kunden angelegt. Bitte beim Personal melden.")
            st.caption("No customers have been set up yet. Please contact staff.")
            st.caption("Hinweis für Personal: über ?staff in der URL gelangt ihr in den Mitarbeiter-Bereich.")
            return

        st.subheader("Wie lautet deine E-Mail-Adresse? / What's your email address?")
        email_input = st.text_input(
            "Bitte gib deine hinterlegte E-Mail-Adresse ein / Please enter your registered email address"
        )

        if email_input:
            match = get_customer_by_email(conn, email_input)
            if match:
                st.session_state.selected_uid = match
                st.rerun()
            else:
                st.error(
                    "E-Mail-Adresse nicht gefunden. Bitte beim Personal melden.\n\n"
                    "Email address not found. Please contact staff."
                )


main_router()

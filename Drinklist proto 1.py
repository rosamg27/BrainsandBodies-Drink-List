import sqlite3  # For database operations
import uuid  # For generating unique customer IDs
from datetime import datetime  # For timestamping transactions
from io import BytesIO  # For handling QR code image data
import qrcode  # For generating QR codes
import streamlit as st  # For creating the web interface
import stripe  # For Stripe payment integration

# Set the Streamlit page configuration
st.set_page_config(page_title="Getränke-Konto", page_icon="🥤", layout="centered")

# Configuration Setup

# Define the database path
DB_PATH = "getraenke.db"

# Set up the Streamlit page configuration
jls_extract_var = "centered"
st.set_page_config(page_title="Getränke-Konto", page_icon="🥤", layout="centered")

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

# Function to establish a connection to the SQLite database and create necessary tables
def initialize_database():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)  # Connect to the database
    # Create the customers table if it doesn't exist
    conn.execute(
        """CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY,  -- Unique customer ID
            name TEXT NOT NULL,  -- Customer name
            credits INTEGER NOT NULL DEFAULT 0,  -- Remaining credits
            created_at TEXT NOT NULL  -- Timestamp of customer creation
        )"""
    )
    # Create the transactions table if it doesn't exist
    conn.execute(
        """CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,  -- Transaction ID
            customer_id TEXT NOT NULL,  -- Associated customer ID
            type TEXT NOT NULL,  -- Transaction type ('kauf' or 'verbrauch')
            amount INTEGER NOT NULL,  -- Amount (+ for purchase, - for consumption)
            timestamp TEXT NOT NULL,  -- Timestamp of the transaction
            employee TEXT,  -- Employee name (optional)
            stripe_session_id TEXT  -- Stripe session ID (optional)
        )"""
    )
    conn.commit()  # Commit changes to the database
    conn.close()  # Close the database connection

# Call the function to initialize the database
initialize_database()

# Function to retrieve customer data by their unique ID
def get_customer(conn, uid):
    """
    Retrieve customer details from the database using their unique ID.

    Args:
        conn: SQLite database connection object.
        uid: Unique customer ID.

    Returns:
        A tuple containing customer details (id, name, credits) or None if not found.
    """
    row = conn.execute(
        "SELECT id, name, credits FROM customers WHERE id = ?", (uid,)
    ).fetchone()
    return row

# Function to create a new customer in the database
def create_customer(conn, name):
    """
    Create a new customer in the database.

    Args:
        conn: SQLite database connection object.
        name: Name of the new customer.

    Returns:
        The unique ID of the newly created customer.
    """
    uid = uuid.uuid4().hex[:10]  # Generate a unique 10-character ID
    conn.execute(
        "INSERT INTO customers (id, name, credits, created_at) VALUES (?, ?, 0, ?)",
        (uid, name, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()  # Commit the changes to the database
    return uid

def log_transaction(conn, customer_id, ttype, amount, employee=None, session_id=None):
    """
    Log a transaction in the database.

    Args:
        conn: SQLite database connection object.
        customer_id: Unique ID of the customer.
        ttype: Type of transaction ('kauf' or 'verbrauch').
        amount: Amount of the transaction (+ for purchase, - for consumption).
        employee: Name of the employee (optional, for staff corrections).
        session_id: Stripe session ID (optional, for purchase transactions).
    """
    conn.execute(
        """INSERT INTO transactions (customer_id, type, amount, timestamp, employee, stripe_session_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (customer_id, ttype, amount, datetime.now().isoformat(timespec="seconds"), employee, session_id),
    )
    conn.commit()

def deduct_drink(conn, customer_id, employee_name):
    """
    Deduct one drink from the customer's credits.

    Args:
        conn: SQLite database connection object.
        customer_id: Unique ID of the customer.
        employee_name: Name of the employee performing the deduction.
    """
    conn.execute(
        "UPDATE customers SET credits = credits - 1 WHERE id = ? AND credits > 0",
        (customer_id,),
    )
    conn.commit()
    log_transaction(conn, customer_id, "verbrauch", -1, employee=employee_name)
    
    # Function to check if a Stripe session has already been processed
def already_processed(conn, session_id):
    """
    Check if a Stripe session has already been processed.

    Args:
        conn: SQLite database connection object.
        session_id: Stripe session ID.

    Returns:
        True if the session has been processed, False otherwise.
    """
    row = conn.execute(
        "SELECT 1 FROM transactions WHERE stripe_session_id = ?", (session_id,)
    ).fetchone()
    return row is not None

# Function to synchronize Stripe payment and update customer credits
def sync_stripe_payment(conn, uid, session_id):
    """
    Synchronize Stripe payment and update customer credits.

    Args:
        conn: SQLite database connection object.
        uid: Unique ID of the customer.
        session_id: Stripe session ID.
    """
    if already_processed(conn, session_id):
        return  # Skip if the session has already been processed

    try:
        session = stripe.checkout.Session.retrieve(session_id)  # Retrieve the session from Stripe
    except Exception as e:
        st.error(f"Zahlung konnte nicht überprüft werden: {e}")
        return

    if session.payment_status == "paid" and session.client_reference_id == uid:
        credits = int(session.metadata.get("credits", 0))  # Get the number of credits purchased
        conn.execute(
            "UPDATE customers SET credits = credits + ? WHERE id = ?", (credits, uid)
        )
        conn.commit()
        log_transaction(conn, uid, "kauf", credits, session_id=session_id)
        st.success(f"Zahlung erfolgreich! {credits} Getränke wurden gutgeschrieben.")

# Function to create a Stripe checkout session
def create_checkout_session(uid, block_key):
    """
    Create a Stripe checkout session for purchasing credits.

    Args:
        uid: Unique ID of the customer.
        block_key: Key representing the block size (e.g., "1", "5", "10").

    Returns:
        The URL of the Stripe checkout session.
    """
    block = BLOCKS[block_key]  # Retrieve block details
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
    """
    Generate a QR code for a given link.

    Args:
        link: The URL or link to encode in the QR code.

    Returns:
        BytesIO object containing the QR code image in PNG format.
    """
    qr = qrcode.QRCode(
        version=1,  # Controls the size of the QR Code
        error_correction=qrcode.constants.ERROR_CORRECT_L,  # Error correction level
        box_size=10,  # Size of each box in the QR code grid
        border=4,  # Thickness of the border (minimum is 4)
    )
    qr.add_data(link)  # Add the link data to the QR code
    qr.make(fit=True)  # Generate the QR code

    # Create an image from the QR code
    img = qr.make_image(fill_color="black", back_color="white")

    # Save the image to a BytesIO object
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)  # Reset the buffer position to the beginning

    return buffer

# Example usage
if SECRETS_OK:
    example_link = f"{APP_URL}/?uid=example_customer_id"
    qr_code_image = generate_qr_code(example_link)
    st.image(qr_code_image, caption="Generated QR Code", use_column_width=True)
    
def customer_view(conn, uid):
    """
    Render the customer-facing interface to view credits, deduct drinks, and purchase blocks.

    Args:
        conn: SQLite database connection object.
        uid: Unique ID of the customer.
    """
    customer = get_customer(conn, uid)
    if not customer:
        st.error("Unbekannter Code. Bitte beim Personal melden.")
        return

    _, name, credits = customer
    st.title(f"Hallo, {name}! 👋")

    # Display the current credit balance as a stamp pass
    filled = min(credits, 10)
    empty = max(10 - filled, 0) if credits <= 10 else 0
    if credits > 10:
        st.markdown(f"### 🥤 {credits} Getränke übrig")
    else:
        stamps = "🟢 " * filled + "⚪ " * empty
        st.markdown(f"### {stamps}")
        st.caption(f"{filled} von 10 Getränken übrig")

    # Display warnings or info based on credit balance
    if credits <= 0:
        st.warning("Dein Guthaben ist aufgebraucht – bitte neuen Block kaufen.")
    elif credits <= 2:
        st.info("Dein Guthaben ist bald aufgebraucht.")

    st.divider()

    # Button to deduct a drink
    if st.button("🥤 Getränk genommen (−1)", disabled=credits <= 0, type="primary", use_container_width=True):
        deduct_drink(conn, uid, employee_name=None)
        st.success("Verbucht, guten Durst!")
        st.experimental_rerun()

    st.caption("Bitte erst klicken, nachdem du dir das Getränk aus dem Kühlschrank genommen hast.")

    st.divider()
    st.subheader("Neuen Block kaufen")

    # Display purchase options
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

    # Display transaction history
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
    """
    Render the staff interface for managing customers, viewing logs, and correcting transactions.

    Args:
        conn: SQLite database connection object.
    """
    st.title("🔑 Mitarbeiter-Bereich")

    if "staff_ok" not in st.session_state:
        st.session_state.staff_ok = False

    # Staff authentication
    if not st.session_state.staff_ok:
        pin = st.text_input("PIN", type="password")
        if st.button("Anmelden"):
            if SECRETS_OK and pin == STAFF_PIN:
                st.session_state.staff_ok = True
                st.experimental_rerun()
            else:
                st.error("Falscher PIN.")
        return

    employee_name = st.text_input("Dein Name (für das Protokoll, nur bei Korrekturen nötig)", key="emp_name")

    tab1, tab2, tab3 = st.tabs(["Verlauf (Kontrolle)", "Neuer Kunde + QR-Code", "Korrektur"])

    # Tab 1: Transaction log
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

    # Tab 2: Add new customer and generate QR code
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
        png = generate_qr_code(APP_URL).getvalue()
        st.image(png, caption="QR-Code für den Kühlschrank (für alle gleich)", width=250)
        st.download_button(
            "Kühlschrank-QR-Code herunterladen", data=png,
            file_name="qr_kuehlschrank.png", mime="image/png",
        )
        st.code(APP_URL)

    # Tab 3: Correct transactions
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
                st.experimental_rerun()
            if not employee_name:
                st.caption("Bitte oben deinen Namen eintragen.")

    # Logout button
    if st.button("Abmelden"):
        st.session_state.staff_ok = False
        st.experimental_rerun()
def main_router():
    """
    Main routing logic to switch between customer and staff views.

    This function determines the view to render based on query parameters or session state.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)  # Establish database connection
    params = st.get_query_params()  # Retrieve query parameters

    # Handle Stripe payment synchronization
    if "session_id" in params and "uid" in params and SECRETS_OK:
        sync_stripe_payment(conn, params["uid"][0], params["session_id"][0])

    # Route to staff view if "staff" parameter is present
    if "staff" in params:
        staff_view(conn)

    # Route to customer view if "uid" parameter is present
    elif "uid" in params:
        customer_view(conn, params["uid"][0])

    # Route to customer view if a user is selected in session state
    elif st.session_state.get("selected_uid"):
        if st.button("← Andere Person"):
            st.session_state.selected_uid = None
            st.experimental_rerun()
        customer_view(conn, st.session_state.selected_uid)

    # Default to name selection view
    else:
        rows = conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
        if not rows:
            st.info("Es sind noch keine Kunden angelegt. Bitte beim Personal melden.")
            return

        names = {n: cid for cid, n in rows}
        choice = st.selectbox(
            "Wer bist du?", options=["– bitte auswählen –"] + list(names.keys())
        )
        if choice != "– bitte auswählen –":
            st.session_state.selected_uid = names[choice]
            st.experimental_rerun()

# The following shell command should be executed in the terminal instead:
# source /Users/rosamariagraff/BrainsandBodies-Drink-List/BrainsandBodies-Drink-List/.venv/bin/activate
# /Users/rosamariagraff/BrainsandBodies-Drink-List/BrainsandBodies-Drink-List/.venv/bin/python "/Users/rosamariagraff/BrainsandBodies-Drink-List/BrainsandBodies-Drink-List/Drinklist proto 1.py"


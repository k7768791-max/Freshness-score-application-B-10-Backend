from flask import Flask, request, jsonify, session
from flask_cors import CORS
import json
import os
import secrets
import joblib
import pandas as pd
import razorpay
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ── Firebase ──
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "stable_dev_secret_key_123!")
CORS(app, supports_credentials=True, origins=[
    "http://localhost:5173", 
    "http://localhost:5174", 
    "http://localhost:3000",
    "https://freshness-score-application-b-10.vercel.app"
])


# ── Razorpay Configuration ──
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_SXlDqkxIKFuAw8")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "sQ6tZElMkvkBqjvuRJSGWMlt")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ── Firebase Initialization from .env ──
def initialize_firebase():
    """Initialize Firebase using credentials from environment variables."""
    try:
        # Build Firebase credentials dictionary from environment variables
        firebase_cred_dict = {
            "type": os.getenv("FIREBASE_TYPE"),
            "project_id": os.getenv("FIREBASE_PROJECT_ID"),
            "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
            "private_key": os.getenv("FIREBASE_PRIVATE_KEY").replace('\\n', '\n'),
            "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
            "client_id": os.getenv("FIREBASE_CLIENT_ID"),
            "auth_uri": os.getenv("FIREBASE_AUTH_URI"),
            "token_uri": os.getenv("FIREBASE_TOKEN_URI"),
            "auth_provider_x509_cert_url": os.getenv("FIREBASE_AUTH_PROVIDER_X509_CERT_URL"),
            "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_X509_CERT_URL"),
            "universe_domain": os.getenv("FIREBASE_UNIVERSE_DOMAIN")
        }
        
        cred = credentials.Certificate(firebase_cred_dict)
        firebase_admin.initialize_app(cred)
        return True
    except Exception as e:
        return False

# Initialize Firebase if all required env vars are present
if os.getenv("FIREBASE_PROJECT_ID") and os.getenv("FIREBASE_PRIVATE_KEY"):
    try:
        if initialize_firebase():
            db = firestore.client()
            USE_FIREBASE = True
            print("✓ Firebase Firestore connected successfully.")
        else:
            print("✗ Firebase init failed.")
            print("  → Falling back to in-memory mode.")
            USE_FIREBASE = False
    except Exception as e:
        print(f"✗ Firebase init failed: {e}")
        print("  → Falling back to in-memory mode.")
        USE_FIREBASE = False
else:
    print("⚠ Firebase credentials not found in .env file. Using in-memory mode.")
    print("  → Ensure your .env file contains all required FIREBASE_* variables")
    USE_FIREBASE = False

# ── In-memory fallback (used when Firebase is not configured) ──
USERS_MEMORY = {
    "admin@freshsense.ai": {
        "fname": "Admin", "lname": "User", "email": "admin@freshsense.ai",
        "password": "admin123", "trial_used": 0,
        "is_pro": True, "is_admin": True, "history": []
    }
}

# ── Load Models ──
freshness_model = None
discount_model = None

try:
    freshness_model = joblib.load("freshness_model.pkl")
    discount_model = joblib.load("discount_model.pkl")
    print("✓ Models loaded successfully.")
except Exception as e:
    print(f"⚠ Warning: Could not load models. Error: {e}")
    print("Simulation mode will be used if models fail.")

# ════════ DATABASE HELPERS ════════

ADMIN_DEFAULT = {
    "fname": "Admin", "lname": "User", "email": "admin@freshsense.ai",
    "password": "admin123", "trial_used": 0,
    "is_pro": True, "is_admin": True
}

def _bootstrap_admin():
    """Ensure admin account exists in Firestore on first boot."""
    if USE_FIREBASE:
        try:
            doc = db.collection("users").document("admin@freshsense.ai").get()
            if not doc.exists:
                db.collection("users").document("admin@freshsense.ai").set(ADMIN_DEFAULT)
                print("✓ Admin account created in Firestore.")
            else:
                print("✓ Admin account already exists in Firestore.")
        except Exception as e:
            print(f"✗ Firestore bootstrap error: {e}")

def get_user(email):
    """Fetch a user dict by email. Returns None if not found."""
    if USE_FIREBASE:
        try:
            doc = db.collection("users").document(email).get()
            if not doc.exists:
                return None
            data = doc.to_dict()
            # Fetch history sub-collection (no order_by — avoids index requirement)
            history_ref = db.collection("users").document(email).collection("history").stream()
            history_list = []
            for h in history_ref:
                h_data = h.to_dict()
                history_list.append(h_data)
            # Sort by timestamp descending in Python
            history_list.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
            data["history"] = history_list
            return data
        except Exception as e:
            print(f"✗ Firestore get_user error for {email}: {e}")
            return None
    else:
        return USERS_MEMORY.get(email)

def save_user(email, data, push_history_entry=None):
    """
    Save user data. If push_history_entry is provided, also add it to
    the Firestore history sub-collection.
    """
    if USE_FIREBASE:
        try:
            # Save main user doc (without history list)
            user_doc = {k: v for k, v in data.items() if k != "history"}
            db.collection("users").document(email).set(user_doc)
        except Exception as e:
            print(f"✗ Firestore save_user error for {email}: {e}")
            return

        if push_history_entry:
            try:
                entry = dict(push_history_entry)
                entry["timestamp"] = datetime.utcnow().isoformat()
                ref = db.collection("users").document(email).collection("history").add(entry)
                print(f"✓ History entry saved for {email}")
            except Exception as e:
                print(f"✗ Firestore history write error for {email}: {e}")
    else:
        USERS_MEMORY[email] = data

def get_all_users_for_admin():
    """Fetch all users for admin panel. Returns list of user dicts."""
    if USE_FIREBASE:
        users = []
        try:
            docs = db.collection("users").stream()
            for doc in docs:
                u = doc.to_dict()
                email_key = u.get("email", doc.id)
                try:
                    history_count = len(list(
                        db.collection("users").document(email_key).collection("history").stream()
                    ))
                except:
                    history_count = 0
                users.append({
                    "fname": u.get("fname", ""),
                    "lname": u.get("lname", ""),
                    "email": email_key,
                    "trial_used": u.get("trial_used", 0),
                    "is_pro": u.get("is_pro", False),
                    "is_admin": u.get("is_admin", False),
                    "history_count": history_count
                })
        except Exception as e:
            print(f"✗ Firestore get_all_users error: {e}")
        return users
    else:
        result = []
        for u in USERS_MEMORY.values():
            result.append({
                "fname": u["fname"], "lname": u["lname"], "email": u["email"],
                "trial_used": u["trial_used"], "is_pro": u["is_pro"],
                "is_admin": u.get("is_admin", False),
                "history_count": len(u.get("history", []))
            })
        return result

# Bootstrap admin on startup
_bootstrap_admin()


# ════════ AUTH ════════

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json()
    fname    = (data.get("fname") or "").strip()
    lname    = (data.get("lname") or "").strip()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not fname or not email or not password:
        return jsonify({"error": "Please fill all fields."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    if get_user(email):
        return jsonify({"error": "Account already exists. Please log in."}), 409

    new_user = {
        "fname": fname, "lname": lname, "email": email,
        "password": password, "trial_used": 0,
        "is_pro": False, "is_admin": False, "history": []
    }
    save_user(email, new_user)
    session["email"] = email
    return jsonify({
        "message": f"Welcome, {fname}! You have 3 free predictions.",
        "user": {"fname": fname, "lname": lname, "email": email,
                 "trial_used": 0, "is_pro": False, "is_admin": False}
    }), 201


@app.route("/api/login", methods=["POST"])
def login():
    data     = request.get_json()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Please enter email and password."}), 400

    user = get_user(email)
    if not user or user["password"] != password:
        return jsonify({"error": "Invalid email or password."}), 401

    session["email"] = email
    return jsonify({
        "message": f"Welcome back, {user['fname']}!",
        "user": {
            "fname": user["fname"], "lname": user["lname"], "email": email,
            "trial_used": user["trial_used"], "is_pro": user["is_pro"],
            "is_admin": user.get("is_admin", False)
        }
    }), 200


@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("email", None)
    return jsonify({"message": "Signed out successfully."}), 200


@app.route("/api/me", methods=["GET"])
def me():
    email = session.get("email")
    if not email:
        return jsonify({"user": None}), 200
    user = get_user(email)
    if not user:
        return jsonify({"user": None}), 200
    return jsonify({
        "user": {
            "fname": user["fname"], "lname": user["lname"], "email": email,
            "trial_used": user["trial_used"], "is_pro": user["is_pro"],
            "is_admin": user.get("is_admin", False)
        }
    }), 200


# ════════ FRESHNESS MODEL ════════

def estimate_shelf_life(category, score, days_since):
    if score >= 75:
        status = "High"
        shelf_map = {
            "Vegetables": 15 if days_since <= 2 else (12 if days_since <= 5 else 10),
            "Dairy":       2 if days_since <= 3 else 1,
            "Fruits":      5 if days_since <= 4 else 4,
            "Meat":       10 if days_since <= 2 else 8,
            "Bakery":      4 if days_since <= 2 else 3,
        }
    elif score >= 50:
        status = "Medium"
        shelf_map = {
            "Vegetables": 10 if days_since <= 2 else (7 if days_since <= 5 else 5),
            "Dairy": 1,
            "Fruits":  3 if days_since <= 4 else 2,
            "Meat":    5 if days_since <= 2 else 4,
            "Bakery":  2 if days_since <= 2 else 1,
        }
    else:
        status = "Low"
        shelf_map = {
            "Vegetables": 2 if days_since <= 3 else 1,
            "Dairy": 0,
            "Fruits": 1,
            "Meat":   2 if days_since <= 2 else 1,
            "Bakery": 1,
        }
    remaining = shelf_map.get(category, 3)
    return status, remaining


@app.route("/api/predict/freshness", methods=["POST"])
def predict_freshness():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Please sign in or create an account first."}), 401

    user = get_user(email)
    if not user:
        return jsonify({"error": "User session active but not found in database. Please log in again."}), 404

    if not user.get("is_pro") and user.get("trial_used", 0) >= 3:
        return jsonify({"error": "trial_exhausted"}), 403

    data        = request.get_json()
    category    = data.get("category", "Vegetables")
    days        = int(data.get("days_since_arrival", 7))
    storage     = data.get("storage", "fridge")
    condition   = data.get("condition", "good")
    packaging   = data.get("packaging", "sealed")
    display     = data.get("display", "fridge")
    damaged     = data.get("damaged", "no")
    weather     = data.get("weather", "normal")
    sensitivity = data.get("sensitivity", "low")
    demand      = data.get("demand", "medium")

    if freshness_model:
        try:
            input_df = pd.DataFrame([{
                "category": category, "days_since_arrival": days,
                "storage": storage, "condition": condition,
                "packaging": packaging, "display": display,
                "damaged": damaged, "weather": weather,
                "sensitivity": sensitivity, "demand": demand
            }])
            score = float(freshness_model.predict(input_df)[0])
        except Exception as e:
            print(f"Prediction Error: {e}")
            score = 80.0 - (days * 3.5)
    else:
        score = 80.0
        score -= days * 3.5
        if condition == "average": score -= 8
        if condition == "poor":    score -= 20
        if packaging == "open":    score -= 10
        if damaged == "yes":       score -= 18
        if weather == "hot":       score -= 12
        if weather == "cold":      score += 5
        if storage == "fridge":    score += 5
        if storage == "freezer":   score += 10
        if storage == "room_temp": score -= 5
        if sensitivity == "high":  score -= 8
        if sensitivity == "low":   score += 5
        if category == "Dairy":    score -= 5
        if category == "Meat":     score -= 3
        if category == "Bakery":   score -= 4

    score = max(5.0, min(99.0, score))
    score = round(score, 2)
    status, remaining = estimate_shelf_life(category, score, days)

    history_entry = {
        "type": "freshness", "category": category,
        "score": score, "status": status, "remaining": remaining
    }

    if not user["is_pro"]:
        user["trial_used"] += 1

    save_user(email, user, push_history_entry=history_entry)

    return jsonify({
        "score": score, "status": status,
        "days_since_arrival": days, "remaining_shelf_life": remaining,
        "trial_used": user["trial_used"], "is_pro": user["is_pro"],
        "discount_prefill": {"freshness": score, "days_arrival": days, "expiry": remaining}
    }), 200


# ════════ DISCOUNT MODEL ════════

@app.route("/api/predict/discount", methods=["POST"])
def predict_discount():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Please sign in or create an account first."}), 401

    user = get_user(email)
    if not user:
        return jsonify({"error": "User session active but not found in database. Please log in again."}), 404

    if not user.get("is_pro") and user.get("trial_used", 0) >= 3:
        return jsonify({"error": "trial_exhausted"}), 403

    data       = request.get_json()
    freshness  = float(data.get("freshness", 65))
    expiry     = int(data.get("days_to_expiry", 3))
    orig_price = float(data.get("original_price", 41.61))
    cost_price = float(data.get("cost_price", 31.33))
    stock      = int(data.get("stock", 243))
    sales      = int(data.get("daily_sales", 41))
    demand     = data.get("demand", "medium")
    season     = data.get("season", "normal")

    if discount_model:
        try:
            input_df = pd.DataFrame([{
                "freshness": freshness, "days_to_expiry": expiry,
                "original_price": orig_price, "cost_price": cost_price,
                "stock_level": stock, "daily_sales": sales,
                "demand_level": demand, "season": season
            }])
            discount = float(discount_model.predict(input_df)[0])
        except Exception as e:
            print(f"Discount Prediction Error: {e}")
            discount = 15.0
    else:
        stock_days = (stock / sales) if sales > 0 else 999
        margin     = ((orig_price - cost_price) / orig_price) if orig_price > 0 else 0.3
        discount   = 0.0
        if freshness < 40:  discount += 30
        elif freshness < 60: discount += 18
        elif freshness < 75: discount += 8
        if expiry <= 1:    discount += 25
        elif expiry <= 3:  discount += 15
        elif expiry <= 7:  discount += 8
        if stock_days > expiry * 1.5: discount += 12
        if demand == "low":   discount += 8
        if demand == "high":  discount -= 8
        if season == "summer": discount -= 3
        if season == "rainy":  discount += 5
        discount = max(0.0, min(margin * 100 * 0.9, discount))

    discount    = round(max(0.0, discount), 2)
    final_price = round(orig_price - (orig_price * discount / 100), 2)

    history_entry = {
        "type": "discount", "freshness": freshness,
        "discount": discount, "final_price": final_price
    }

    if not user["is_pro"]:
        user["trial_used"] += 1

    save_user(email, user, push_history_entry=history_entry)

    return jsonify({
        "discount": discount, "final_price": final_price,
        "trial_used": user["trial_used"], "is_pro": user["is_pro"]
    }), 200


# ════════ DASHBOARD ════════

@app.route("/api/dashboard/stats", methods=["GET"])
def dashboard_stats():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Unauthorized"}), 401

    user = get_user(email)
    if not user:
        return jsonify({"total_predictions": 0, "avg_freshness": 0, "category_breakdown": {}}), 200

    if USE_FIREBASE:
        try:
            # Query history sub-collection
            history_ref = db.collection("users").document(email).collection("history")
            docs = history_ref.stream()
            history = [doc.to_dict() for doc in docs]
        except Exception as e:
            print(f"✗ Firestore stats error: {e}")
            history = []
    else:
        history = user.get("history", [])

    freshness_entries = [h for h in history if h.get("type") == "freshness"]
    avg_freshness = round(sum(h["score"] for h in freshness_entries) / len(freshness_entries), 1) if freshness_entries else 0

    cats = {}
    for h in freshness_entries:
        cat = h.get("category", "Other")
        cats[cat] = cats.get(cat, 0) + 1

    return jsonify({
        "total_predictions": len(history),
        "avg_freshness": avg_freshness,
        "category_breakdown": cats,
        "is_pro": user.get("is_pro", False)
    }), 200


@app.route("/api/dashboard/history", methods=["GET"])
def dashboard_history():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Unauthorized"}), 401

    if USE_FIREBASE:
        try:
            # Get latest 20 entries from sub-collection
            docs = db.collection("users").document(email).collection("history")\
                     .order_by("timestamp", direction=firestore.Query.DESCENDING)\
                     .limit(20).get()
            history = [doc.to_dict() for doc in docs]
        except Exception as e:
            print(f"✗ Firestore history fetch error: {e}")
            history = []
    else:
        user = get_user(email)
        history = user.get("history", [])[::-1][:20] if user else []

    return jsonify(history), 200


# ════════ ADMIN ════════

@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Unauthorized"}), 401

    user = get_user(email)
    if not user or not user.get("is_admin"):
        return jsonify({"error": "Forbidden"}), 403

    all_users  = get_all_users_for_admin()
    total_users = len(all_users)
    pro_users   = sum(1 for u in all_users if u.get("is_pro") and not u.get("is_admin"))
    total_rev   = pro_users * 499
    total_pred  = sum(u.get("history_count", 0) for u in all_users)

    # Category stats across all users
    cat_stats = {}
    if USE_FIREBASE:
        try:
            # Note: In a real large app, you'd use a counter. For now, we iterate users.
            for u in all_users:
                email = u["email"]
                docs = db.collection("users").document(email).collection("history").stream()
                for doc in docs:
                    h = doc.to_dict()
                    if h.get("type") == "freshness":
                        cat = h.get("category", "Other")
                        cat_stats[cat] = cat_stats.get(cat, 0) + 1
        except Exception as e:
            print(f"✗ Admin stats category error: {e}")
    else:
        for u in USERS_MEMORY.values():
            for h in u.get("history", []):
                if h["type"] == "freshness":
                    cat = h.get("category", "Other")
                    cat_stats[cat] = cat_stats.get(cat, 0) + 1

    return jsonify({
        "total_users": total_users,
        "pro_users": pro_users,
        "total_revenue": total_rev,
        "total_predictions": total_pred,
        "category_usage": cat_stats
    }), 200


@app.route("/api/admin/users", methods=["GET"])
def admin_users():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Unauthorized"}), 401

    user = get_user(email)
    if not user or not user.get("is_admin"):
        return jsonify({"error": "Forbidden"}), 403

    return jsonify(get_all_users_for_admin()), 200


# ════════ PAYMENT ════════

@app.route("/api/payment/initiate", methods=["POST"])
def payment_initiate():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Not authenticated"}), 401

    data   = request.get_json()
    plan   = data.get("plan", "monthly")
    amount = 49900 if plan == "monthly" else 399900

    try:
        order = razorpay_client.order.create(data={
            "amount": amount, "currency": "INR",
            "receipt": f"receipt_{plan}_{secrets.token_hex(4)}",
            "payment_capture": 1
        })
        return jsonify({
            "order_id": order["id"], "amount": amount,
            "currency": "INR", "key": RAZORPAY_KEY_ID
        }), 200
    except Exception as e:
        print(f"Razorpay Order Error: {e}")
        return jsonify({"error": "Could not initiate payment. Please try again."}), 500


@app.route("/api/payment/success", methods=["POST"])
def payment_success():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Not authenticated"}), 401

    data                = request.get_json()
    razorpay_order_id   = data.get("razorpay_order_id")
    razorpay_payment_id = data.get("razorpay_payment_id")
    razorpay_signature  = data.get("razorpay_signature")

    if not razorpay_order_id or not razorpay_payment_id or not razorpay_signature:
        return jsonify({"error": "Missing payment verification details."}), 400

    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature
        })
        user = get_user(email)
        if user:
            user["is_pro"] = True
            save_user(email, user)

        return jsonify({
            "message": "🎉 Welcome to Pro! Unlimited predictions unlocked.",
            "user": {
                "fname": user["fname"], "lname": user["lname"],
                "email": email, "trial_used": user["trial_used"],
                "is_pro": True
            }
        }), 200
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"error": "Invalid payment signature."}), 400
    except Exception as e:
        print(f"Payment Success Error: {e}")
        return jsonify({"error": "Something went wrong during verification."}), 500


# ════════ CONTACT ════════

@app.route("/api/contact", methods=["POST"])
def contact():
    data  = request.get_json()
    fname = (data.get("fname") or "").strip()
    email = (data.get("email") or "").strip()
    msg   = (data.get("message") or "").strip()

    if not fname or not email or not msg:
        return jsonify({"error": "Please fill all required fields."}), 400

    if USE_FIREBASE:
        db.collection("contact_messages").add({
            "fname": fname, "email": email, "message": msg,
            "timestamp": datetime.utcnow().isoformat()
        })

    print(f"[CONTACT] {fname} <{email}>: {msg[:80]}")
    return jsonify({"message": "✓ Message sent! We'll get back to you within 24 hours."}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)

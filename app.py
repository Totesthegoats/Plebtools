from flask import Flask, request, jsonify, session, render_template, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import json
import time

import pandas as pd
import requests

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv not installed, continue without it
    pass

# Ensure data directory exists for cPanel storage
os.makedirs('data', exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///data/plebtools.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Email configuration
MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
MAIL_USE_TLS = True

db = SQLAlchemy(app)

# Fear & Greed data configuration
FNG_API_URL = "https://api.alternative.me/fng/?limit=0"
BINANCE_API_URL = "https://api.binance.com/api/v3/klines"
BINANCE_SYMBOL = "BTCUSDT"
BINANCE_INTERVAL = "1d"
BINANCE_LIMIT = 1000
BINANCE_START = pd.Timestamp("2017-08-17", tz="UTC")

FNG_BUCKETS = [
    {"label": "fear", "min": 0, "max": 20},
    {"label": "neutral", "min": 21, "max": 79},
    {"label": "greed", "min": 80, "max": 100},
]

DEFAULT_BUCKET_WEIGHTS = {
    "fear": 3.0,
    "neutral": 1.0,
    "greed": 0.5,
}

NORMALIZE_FNG_WEIGHTS = True

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_verified = db.Column(db.Boolean, default=False)
    newsletter_subscribed = db.Column(db.Boolean, default=False)
    verification_token = db.Column(db.String(100), nullable=True)
    
    # Relationship to BTC purchases
    btc_purchases = db.relationship('BTCPurchase', backref='user', lazy=True, cascade='all, delete-orphan')
    wallet_addresses = db.relationship('WalletAddress', backref='user', lazy=True, cascade='all, delete-orphan')
    covered_call_trades = db.relationship('CoveredCallTrade', backref='user', lazy=True, cascade='all, delete-orphan')

class BTCPurchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD format
    btc_amount = db.Column(db.Float, nullable=False)
    price_usd = db.Column(db.Float, nullable=False)
    original_price = db.Column(db.Float, nullable=True)
    original_currency = db.Column(db.String(3), nullable=True)  # USD or CAD
    transaction_type = db.Column(db.String(10), default='buy')  # buy or sell
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class WalletAddress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    address = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class CoveredCallTrade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    trade_type = db.Column(db.String(20), nullable=False)  # 'shares' or 'covered-call'
    symbol = db.Column(db.String(10), nullable=False)
    shares = db.Column(db.Integer, nullable=False)
    original_cost_basis = db.Column(db.Float, nullable=False)
    new_cost_basis = db.Column(db.Float, nullable=False)
    strike_price = db.Column(db.Float, nullable=True)
    premium = db.Column(db.Float, nullable=True)
    expiration_date = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD format
    current_price = db.Column(db.Float, default=0)
    notes = db.Column(db.Text, nullable=True)
    pnl = db.Column(db.Float, default=0)
    date_added = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# Helper Functions
def send_verification_email(email, username, token):
    """Send verification email to user"""
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        print(f"Email verification token for {username}: {token}")
        return True
    
    try:
        msg = MIMEMultipart()
        msg['From'] = MAIL_USERNAME
        msg['To'] = email
        msg['Subject'] = "Verify Your Plebtools Account"
        
        verification_url = f"https://yourdomain.com/verify?token={token}"
        body = f"""
        Hello {username},
        
        Thank you for creating an account with Plebtools!
        
        Please click the link below to verify your email address:
        {verification_url}
        
        If you didn't create this account, please ignore this email.
        
        Best regards,
        The Plebtools Team
        """
        
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP(MAIL_SERVER, MAIL_PORT)
        server.starttls()
        server.login(MAIL_USERNAME, MAIL_PASSWORD)
        text = msg.as_string()
        server.sendmail(MAIL_USERNAME, email, text)
        server.quit()
        
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

def send_newsletter_welcome(email, username):
    """Send welcome email for newsletter subscription"""
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        print(f"Newsletter welcome for {username} ({email})")
        return True
    
    try:
        msg = MIMEMultipart()
        msg['From'] = MAIL_USERNAME
        msg['To'] = email
        msg['Subject'] = "Welcome to Plebtools Newsletter!"
        
        body = f"""
        Hello {username},
        
        Thank you for subscribing to the Plebtools newsletter!
        
        You'll receive updates about:
        - New features and tools
        - Bitcoin market insights
        - Security tips and best practices
        - Community updates
        
        You can unsubscribe at any time by logging into your account.
        
        Best regards,
        The Plebtools Team
        """
        
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP(MAIL_SERVER, MAIL_PORT)
        server.starttls()
        server.login(MAIL_USERNAME, MAIL_PASSWORD)
        text = msg.as_string()
        server.sendmail(MAIL_USERNAME, email, text)
        server.quit()
        
        return True
    except Exception as e:
        print(f"Error sending newsletter email: {e}")
        return False


# ---------------------------------------------------------------------------
# Fear & Greed - weighted DCA utilities
# ---------------------------------------------------------------------------

def fetch_fear_greed_history() -> pd.DataFrame:
    """Fetch full Fear & Greed index history."""
    resp = requests.get(FNG_API_URL, timeout=30)
    resp.raise_for_status()
    payload = resp.json().get("data", [])
    if not payload:
        raise ValueError("Fear & Greed API returned no data.")

    df = pd.DataFrame(payload)
    df["timestamp"] = df["timestamp"].astype("int64")
    df["date"] = pd.to_datetime(df["timestamp"], unit="s").dt.normalize()
    df["value"] = df["value"].astype(int)
    df = df[["date", "value"]].dropna().drop_duplicates("date").sort_values("date")
    return df.reset_index(drop=True)


def fetch_btc_history(start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    """Fetch BTC daily closes from Binance within a date window."""
    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)

    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    else:
        start_ts = start_ts.tz_convert("UTC")

    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    else:
        end_ts = end_ts.tz_convert("UTC")

    end_ts = end_ts + pd.Timedelta(days=1)

    if start_ts < BINANCE_START:
        start_ts = BINANCE_START

    if end_ts <= start_ts:
        raise ValueError("End date must be after start date.")

    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)
    rows = []

    while start_ms < end_ms:
        params = {
            "symbol": BINANCE_SYMBOL,
            "interval": BINANCE_INTERVAL,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": BINANCE_LIMIT,
        }
        resp = requests.get(BINANCE_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        rows.extend(batch)
        last_open = batch[-1][0]
        next_start = last_open + 24 * 60 * 60 * 1000
        if next_start <= start_ms:
            break
        start_ms = next_start
        time.sleep(0.1)

    if not rows:
        raise ValueError("No BTC price data returned for the selected period.")

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbav", "tbqv", "ignore"
    ]
    df = pd.DataFrame(rows, columns=cols)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms").dt.normalize()
    df["price"] = df["close"].astype(float)
    df = df[["date", "price"]].drop_duplicates("date").sort_values("date")
    return df.reset_index(drop=True)


def _assign_weight(value: int, weights: dict) -> float:
    for bucket in FNG_BUCKETS:
        if bucket["min"] <= value <= bucket["max"]:
            return weights.get(bucket["label"], 1.0)
    return 1.0


def prepare_frequency_frame(start_date: str, end_date: str, frequency: str, bucket_weights: dict | None = None) -> pd.DataFrame:
    """Merge BTC and Fear & Greed data, resampled to the requested cadence."""
    if frequency not in {"daily", "weekly", "monthly"}:
        raise ValueError("Frequency must be daily, weekly, or monthly.")

    start_dt = pd.to_datetime(start_date).normalize()
    end_dt = pd.to_datetime(end_date).normalize()
    if start_dt >= end_dt:
        raise ValueError("Start date must be before end date.")

    fng_df = fetch_fear_greed_history()
    price_df = fetch_btc_history(start_dt, end_dt)
    merged = pd.merge(price_df, fng_df, on="date", how="inner")
    merged = merged[(merged["date"] >= start_dt) & (merged["date"] <= end_dt)].copy()

    if merged.empty:
        raise ValueError("No overlapping BTC and Fear & Greed data.")

    rule = None
    agg = {"price": "last", "value": "mean"}
    if frequency == "weekly":
        rule = "W-MON"
    elif frequency == "monthly":
        rule = "M"

    if rule:
        merged = merged.set_index("date").resample(rule).agg(agg).dropna().reset_index()

    weights = bucket_weights or DEFAULT_BUCKET_WEIGHTS
    merged["weight_raw"] = merged["value"].apply(lambda v: _assign_weight(v, weights))
    if NORMALIZE_FNG_WEIGHTS:
        mean_weight = merged["weight_raw"].mean()
        merged["weight"] = merged["weight_raw"] / mean_weight if mean_weight else merged["weight_raw"]
    else:
        merged["weight"] = merged["weight_raw"]

    return merged[["date", "price", "value", "weight"]]


def run_fear_greed_backtest(amount: float, start_date: str, end_date: str, frequency: str, bucket_weights: dict | None = None):
    """Return cumulative series and summary stats for flat vs. weighted DCA."""
    df = prepare_frequency_frame(start_date, end_date, frequency, bucket_weights=bucket_weights).sort_values("date").reset_index(drop=True)

    df["invest_flat"] = amount
    df["btc_flat"] = df["invest_flat"] / df["price"]
    df["cum_btc_flat"] = df["btc_flat"].cumsum()
    df["cum_invest_flat"] = df["invest_flat"].cumsum()

    df["invest_weighted"] = amount * df["weight"]
    df["btc_weighted"] = df["invest_weighted"] / df["price"]
    df["cum_btc_weighted"] = df["btc_weighted"].cumsum()
    df["cum_invest_weighted"] = df["invest_weighted"].cumsum()

    final_price = df["price"].iloc[-1]
    flat_total = df["cum_invest_flat"].iloc[-1]
    weighted_total = df["cum_invest_weighted"].iloc[-1]
    flat_value = df["cum_btc_flat"].iloc[-1] * final_price
    weighted_value = df["cum_btc_weighted"].iloc[-1] * final_price

    summary = {
        "start": df["date"].iloc[0].isoformat(),
        "end": df["date"].iloc[-1].isoformat(),
        "frequency": frequency,
        "points": len(df),
        "final_price": final_price,
        "flat": {
            "total": flat_total,
            "btc": df["cum_btc_flat"].iloc[-1],
            "value": flat_value,
            "roi": flat_value / flat_total - 1 if flat_total else 0,
        },
        "weighted": {
            "total": weighted_total,
            "btc": df["cum_btc_weighted"].iloc[-1],
            "value": weighted_value,
            "roi": weighted_value / weighted_total - 1 if weighted_total else 0,
        },
    }

    summary["weights"] = bucket_weights or DEFAULT_BUCKET_WEIGHTS
    return df, summary


def get_current_weight(frequency: str, bucket_weights: dict | None = None) -> dict:
    """Calculate today's weight based on frequency and current Fear & Greed data."""
    if frequency not in {"daily", "weekly", "monthly"}:
        raise ValueError("Frequency must be daily, weekly, or monthly.")
    
    fng_df = fetch_fear_greed_history()
    if fng_df.empty:
        raise ValueError("No Fear & Greed data available.")
    
    # Ensure dates are normalized (no time component, no timezone)
    fng_df["date"] = pd.to_datetime(fng_df["date"]).dt.normalize()
    
    # Get today's date (normalized, no timezone) - use UTC to match F&G data
    # Convert to match DataFrame column dtype (datetime64[ns])
    # Extract date string and convert to ensure consistent type
    today_date_str = pd.Timestamp.utcnow().normalize().strftime('%Y-%m-%d')
    today = pd.to_datetime(today_date_str)
    
    # Filter to relevant date range based on frequency
    if frequency == "daily":
        # Just today's value, or most recent if today not available
        period_df = fng_df[fng_df["date"] == today].copy()
        if period_df.empty:
            # Use most recent available
            period_df = fng_df.tail(1).copy()
            period_label = f"most recent ({period_df['date'].iloc[0].strftime('%Y-%m-%d')})"
        else:
            period_label = "today"
    elif frequency == "weekly":
        # Past 7 days (rolling average)
        seven_days_ago = pd.to_datetime(today - pd.Timedelta(days=7))
        period_df = fng_df[(fng_df["date"] >= seven_days_ago) & (fng_df["date"] <= today)].copy()
        
        if period_df.empty:
            # Fallback: use most recent 7 days available
            most_recent = fng_df["date"].max()
            seven_days_ago_fallback = pd.to_datetime(most_recent - pd.Timedelta(days=7))
            period_df = fng_df[(fng_df["date"] >= seven_days_ago_fallback) & (fng_df["date"] <= most_recent)].copy()
            if period_df.empty:
                # If still empty, just use most recent value
                period_df = fng_df.tail(1).copy()
                period_label = f"most recent ({period_df['date'].iloc[0].strftime('%Y-%m-%d')})"
            else:
                period_label = f"past 7 days ({period_df['date'].min().strftime('%Y-%m-%d')} to {period_df['date'].max().strftime('%Y-%m-%d')})"
        else:
            period_label = f"past 7 days ({period_df['date'].min().strftime('%Y-%m-%d')} to {period_df['date'].max().strftime('%Y-%m-%d')})"
    else:  # monthly
        # Past 30 days (rolling average)
        thirty_days_ago = pd.to_datetime(today - pd.Timedelta(days=30))
        period_df = fng_df[(fng_df["date"] >= thirty_days_ago) & (fng_df["date"] <= today)].copy()
        
        if period_df.empty:
            # Fallback: use most recent 30 days available
            most_recent = fng_df["date"].max()
            thirty_days_ago_fallback = pd.to_datetime(most_recent - pd.Timedelta(days=30))
            period_df = fng_df[(fng_df["date"] >= thirty_days_ago_fallback) & (fng_df["date"] <= most_recent)].copy()
            if period_df.empty:
                # If still empty, just use most recent value
                period_df = fng_df.tail(1).copy()
                period_label = f"most recent ({period_df['date'].iloc[0].strftime('%Y-%m-%d')})"
            else:
                period_label = f"past 30 days ({period_df['date'].min().strftime('%Y-%m-%d')} to {period_df['date'].max().strftime('%Y-%m-%d')})"
        else:
            period_label = f"past 30 days ({period_df['date'].min().strftime('%Y-%m-%d')} to {period_df['date'].max().strftime('%Y-%m-%d')})"
    
    if period_df.empty:
        # Final fallback: use most recent single value
        period_df = fng_df.tail(1).copy()
        period_label = f"most recent available ({period_df['date'].iloc[0].strftime('%Y-%m-%d')})"
    
    # Calculate average F&G value for the period
    avg_fng = period_df["value"].mean()
    
    # Apply weight
    weights = bucket_weights or DEFAULT_BUCKET_WEIGHTS
    raw_weight = _assign_weight(int(round(avg_fng)), weights)
    
    # For display purposes, we don't normalize (normalization is only for backtests)
    return {
        "frequency": frequency,
        "period_label": period_label,
        "fear_greed_value": float(avg_fng),
        "raw_weight": float(raw_weight),
        "period_dates": [d.strftime("%Y-%m-%d") for d in period_df["date"].tolist()],
    }

# API Routes
@app.route('/api/register', methods=['POST'])
def register():
    """Register a new user"""
    data = request.get_json()
    
    username = data.get('username', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '')
    newsletter_subscribed = data.get('newsletter_subscribed', False)
    
    # Validation
    if not username or len(username) < 3:
        return jsonify({'error': 'Username must be at least 3 characters long'}), 400
    
    if not password or len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters long'}), 400
    
    if email and '@' not in email:
        return jsonify({'error': 'Invalid email address'}), 400
    
    # Check if username already exists
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username already exists'}), 400
    
    # Check if email already exists (if provided)
    if email and User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already registered'}), 400
    
    # Create user
    password_hash = generate_password_hash(password)
    verification_token = secrets.token_urlsafe(32)
    
    user = User(
        username=username,
        email=email if email else None,
        password_hash=password_hash,
        newsletter_subscribed=newsletter_subscribed,
        verification_token=verification_token
    )
    
    try:
        db.session.add(user)
        db.session.commit()
        
        # Send verification email if email provided
        if email:
            send_verification_email(email, username, verification_token)
        
        # Send newsletter welcome if subscribed
        if newsletter_subscribed and email:
            send_newsletter_welcome(email, username)
        
        return jsonify({
            'message': 'Account created successfully',
            'user_id': user.id,
            'email_verification_sent': bool(email)
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to create account'}), 500

@app.route('/api/login', methods=['POST'])
def login():
    """Login user"""
    data = request.get_json()
    
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    
    user = User.query.filter_by(username=username).first()
    
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({'error': 'Invalid username or password'}), 401
    
    # Create session
    session['user_id'] = user.id
    session['username'] = user.username
    
    return jsonify({
        'message': 'Login successful',
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'is_verified': user.is_verified,
            'newsletter_subscribed': user.newsletter_subscribed
        }
    })

@app.route('/api/logout', methods=['POST'])
def logout():
    """Logout user"""
    session.clear()
    return jsonify({'message': 'Logout successful'})

@app.route('/api/verify', methods=['POST'])
def verify_email():
    """Verify user email with token"""
    data = request.get_json()
    token = data.get('token', '').strip()
    
    if not token:
        return jsonify({'error': 'Verification token required'}), 400
    
    user = User.query.filter_by(verification_token=token).first()
    
    if not user:
        return jsonify({'error': 'Invalid verification token'}), 400
    
    user.is_verified = True
    user.verification_token = None
    
    try:
        db.session.commit()
        return jsonify({'message': 'Email verified successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to verify email'}), 500

@app.route('/api/user', methods=['GET'])
def get_user():
    """Get current user info"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    user = User.query.get(session['user_id'])
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    return jsonify({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'is_verified': user.is_verified,
        'newsletter_subscribed': user.newsletter_subscribed,
        'created_at': user.created_at.isoformat()
    })

@app.route('/api/btc-purchases', methods=['GET'])
def get_btc_purchases():
    """Get user's BTC purchases"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    purchases = BTCPurchase.query.filter_by(user_id=session['user_id']).order_by(BTCPurchase.date.desc()).all()
    
    return jsonify([{
        'id': p.id,
        'date': p.date,
        'btc_amount': p.btc_amount,
        'price_usd': p.price_usd,
        'original_price': p.original_price,
        'original_currency': p.original_currency,
        'transaction_type': p.transaction_type,
        'created_at': p.created_at.isoformat()
    } for p in purchases])

@app.route('/api/btc-purchases', methods=['POST'])
def create_btc_purchase():
    """Create new BTC purchase"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    data = request.get_json()
    
    purchase = BTCPurchase(
        user_id=session['user_id'],
        date=data['date'],
        btc_amount=data['btc_amount'],
        price_usd=data['price_usd'],
        original_price=data.get('original_price'),
        original_currency=data.get('original_currency'),
        transaction_type=data.get('transaction_type', 'buy')
    )
    
    try:
        db.session.add(purchase)
        db.session.commit()
        
        return jsonify({
            'id': purchase.id,
            'date': purchase.date,
            'btc_amount': purchase.btc_amount,
            'price_usd': purchase.price_usd,
            'original_price': purchase.original_price,
            'original_currency': purchase.original_currency,
            'transaction_type': purchase.transaction_type,
            'created_at': purchase.created_at.isoformat()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to create purchase'}), 500

@app.route('/api/btc-purchases/<int:purchase_id>', methods=['DELETE'])
def delete_btc_purchase(purchase_id):
    """Delete BTC purchase"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    purchase = BTCPurchase.query.filter_by(id=purchase_id, user_id=session['user_id']).first()
    
    if not purchase:
        return jsonify({'error': 'Purchase not found'}), 404
    
    try:
        db.session.delete(purchase)
        db.session.commit()
        return jsonify({'message': 'Purchase deleted successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to delete purchase'}), 500

@app.route('/api/wallet-addresses', methods=['GET'])
def get_wallet_addresses():
    """Get user's wallet addresses"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    addresses = WalletAddress.query.filter_by(user_id=session['user_id']).order_by(WalletAddress.created_at.desc()).all()
    
    return jsonify([{
        'id': w.id,
        'address': w.address,
        'created_at': w.created_at.isoformat()
    } for w in addresses])

@app.route('/api/wallet-addresses', methods=['POST'])
def create_wallet_address():
    """Add wallet address"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    data = request.get_json()
    address = data.get('address', '').strip()
    
    if not address:
        return jsonify({'error': 'Wallet address required'}), 400
    
    # Check if address already exists for this user
    existing = WalletAddress.query.filter_by(user_id=session['user_id'], address=address).first()
    if existing:
        return jsonify({'error': 'Wallet address already added'}), 400
    
    wallet = WalletAddress(
        user_id=session['user_id'],
        address=address
    )
    
    try:
        db.session.add(wallet)
        db.session.commit()
        
        return jsonify({
            'id': wallet.id,
            'address': wallet.address,
            'created_at': wallet.created_at.isoformat()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to add wallet address'}), 500

@app.route('/api/wallet-addresses/<int:address_id>', methods=['DELETE'])
def delete_wallet_address(address_id):
    """Delete wallet address"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    wallet = WalletAddress.query.filter_by(id=address_id, user_id=session['user_id']).first()
    
    if not wallet:
        return jsonify({'error': 'Wallet address not found'}), 404
    
    try:
        db.session.delete(wallet)
        db.session.commit()
        return jsonify({'message': 'Wallet address deleted successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to delete wallet address'}), 500

@app.route('/api/sync-data', methods=['POST'])
def sync_data():
    """Sync all user data from frontend"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    data = request.get_json()
    purchases = data.get('purchases', [])
    wallet_addresses = data.get('wallet_addresses', [])
    
    try:
        # Clear existing data
        BTCPurchase.query.filter_by(user_id=session['user_id']).delete()
        WalletAddress.query.filter_by(user_id=session['user_id']).delete()
        
        # Add new purchases
        for purchase_data in purchases:
            purchase = BTCPurchase(
                user_id=session['user_id'],
                date=purchase_data['date'],
                btc_amount=purchase_data['btc_amount'],
                price_usd=purchase_data['price_usd'],
                original_price=purchase_data.get('original_price'),
                original_currency=purchase_data.get('original_currency'),
                transaction_type=purchase_data.get('transaction_type', 'buy')
            )
            db.session.add(purchase)
        
        # Add new wallet addresses
        for address_data in wallet_addresses:
            wallet = WalletAddress(
                user_id=session['user_id'],
                address=address_data['address']
            )
            db.session.add(wallet)
        
        db.session.commit()
        
        return jsonify({'message': 'Data synced successfully'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to sync data'}), 500

@app.route('/api/covered-call-trades', methods=['GET'])
def get_covered_call_trades():
    """Get user's covered call trades"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    trades = CoveredCallTrade.query.filter_by(user_id=session['user_id']).order_by(CoveredCallTrade.date_added.desc()).all()
    
    return jsonify([{
        'id': t.id,
        'trade_type': t.trade_type,
        'symbol': t.symbol,
        'shares': t.shares,
        'original_cost_basis': t.original_cost_basis,
        'new_cost_basis': t.new_cost_basis,
        'strike_price': t.strike_price,
        'premium': t.premium,
        'expiration_date': t.expiration_date,
        'current_price': t.current_price,
        'notes': t.notes,
        'pnl': t.pnl,
        'date_added': t.date_added.isoformat(),
        'created_at': t.created_at.isoformat()
    } for t in trades])

@app.route('/api/covered-call-trades', methods=['POST'])
def create_covered_call_trade():
    """Create new covered call trade"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    data = request.get_json()
    
    trade = CoveredCallTrade(
        user_id=session['user_id'],
        trade_type=data['trade_type'],
        symbol=data['symbol'],
        shares=data['shares'],
        original_cost_basis=data['original_cost_basis'],
        new_cost_basis=data['new_cost_basis'],
        strike_price=data.get('strike_price'),
        premium=data.get('premium'),
        expiration_date=data.get('expiration_date'),
        current_price=data.get('current_price', 0),
        notes=data.get('notes'),
        pnl=data.get('pnl', 0),
        date_added=datetime.fromisoformat(data['date_added'].replace('Z', '+00:00')) if data.get('date_added') else datetime.utcnow()
    )
    
    try:
        db.session.add(trade)
        db.session.commit()
        
        return jsonify({
            'id': trade.id,
            'trade_type': trade.trade_type,
            'symbol': trade.symbol,
            'shares': trade.shares,
            'original_cost_basis': trade.original_cost_basis,
            'new_cost_basis': trade.new_cost_basis,
            'strike_price': trade.strike_price,
            'premium': trade.premium,
            'expiration_date': trade.expiration_date,
            'current_price': trade.current_price,
            'notes': trade.notes,
            'pnl': trade.pnl,
            'date_added': trade.date_added.isoformat(),
            'created_at': trade.created_at.isoformat()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to create trade'}), 500

@app.route('/api/covered-call-trades/<int:trade_id>', methods=['DELETE'])
def delete_covered_call_trade(trade_id):
    """Delete covered call trade"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    trade = CoveredCallTrade.query.filter_by(id=trade_id, user_id=session['user_id']).first()
    
    if not trade:
        return jsonify({'error': 'Trade not found'}), 404
    
    try:
        db.session.delete(trade)
        db.session.commit()
        return jsonify({'message': 'Trade deleted successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to delete trade'}), 500


@app.route('/api/fng-dca', methods=['GET'])
def fear_greed_dca_api():
    """Compare flat vs. Fear & Greed-weighted DCA schedules."""
    try:
        amount = float(request.args.get("amount", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount provided."}), 400

    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero."}), 400

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    frequency = (request.args.get("frequency", "daily") or "daily").lower()

    if not start_date:
        return jsonify({"error": "start_date is required (YYYY-MM-DD)."}), 400

    if not end_date:
        end_date = datetime.utcnow().strftime("%Y-%m-%d")

    bucket_weights = DEFAULT_BUCKET_WEIGHTS.copy()
    weight_params = {
        "fear_weight": "fear",
        "neutral_weight": "neutral",
        "greed_weight": "greed",
    }
    for param, label in weight_params.items():
        raw = request.args.get(param)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return jsonify({"error": f"{param} must be a number."}), 400
        if value <= 0:
            return jsonify({"error": f"{param} must be greater than zero."}), 400
        bucket_weights[label] = value

    try:
        backtest_df, summary = run_fear_greed_backtest(amount, start_date, end_date, frequency, bucket_weights=bucket_weights)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Fear & Greed DCA failure: %s", exc)
        return jsonify({"error": "Unable to complete F&G DCA calculation."}), 500

    series = []
    for _, row in backtest_df.iterrows():
        series.append({
            "date": row["date"].strftime("%Y-%m-%d"),
            "price": row["price"],
            "fear_greed": int(row["value"]),
            "weight": float(row["weight"]),
            "flat_invested": float(row["cum_invest_flat"]),
            "weighted_invested": float(row["cum_invest_weighted"]),
            "flat_value": float(row["cum_btc_flat"] * row["price"]),
            "weighted_value": float(row["cum_btc_weighted"] * row["price"]),
        })

    return jsonify({
        "summary": summary,
        "series": series,
        "meta": {
            "amount_per_contribution": amount,
            "frequency": summary["frequency"],
            "contribution_count": len(series),
            "weights": summary.get("weights", bucket_weights),
        }
    })


@app.route('/api/fng-dca/current-weight', methods=['GET'])
def fear_greed_current_weight_api():
    """Get today's weight based on selected frequency and weights."""
    try:
        frequency = (request.args.get("frequency", "daily") or "daily").lower()
        if frequency not in {"daily", "weekly", "monthly"}:
            return jsonify({"error": "Frequency must be daily, weekly, or monthly."}), 400
        
        bucket_weights = None
        fear_weight = request.args.get("fear_weight")
        neutral_weight = request.args.get("neutral_weight")
        greed_weight = request.args.get("greed_weight")
        
        if fear_weight and neutral_weight and greed_weight:
            try:
                bucket_weights = {
                    "fear": float(fear_weight),
                    "neutral": float(neutral_weight),
                    "greed": float(greed_weight),
                }
                if any(w <= 0 for w in bucket_weights.values()):
                    return jsonify({"error": "All weights must be greater than zero."}), 400
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid weight values."}), 400
        
        result = get_current_weight(frequency, bucket_weights)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Current weight calculation failure: %s", exc)
        return jsonify({"error": "Unable to calculate current weight."}), 500

# Static file routes
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/home')
def home():
    return send_from_directory('.', 'home.html')

@app.route('/home.html')
def home_html():
    return send_from_directory('.', 'home.html')

@app.route('/treasury')
def treasury():
    return send_from_directory('.', 'treasury.html')

@app.route('/treasury.html')
def treasury_html():
    return send_from_directory('.', 'treasury.html')

@app.route('/btc-buy-tracker')
def btc_tracker():
    return send_from_directory('.', 'btc-buy-tracker.html')

@app.route('/btc-buy-tracker.html')
def btc_tracker_html():
    return send_from_directory('.', 'btc-buy-tracker.html')

@app.route('/coveredcall-tracker.html')
def covered_call_tracker():
    return send_from_directory('.', 'coveredcall-tracker.html')

@app.route('/press-release')
def press_release():
    return send_from_directory('.', 'pleb-release.html')

@app.route('/pleb-release.html')
def press_release_html():
    return send_from_directory('.', 'pleb-release.html')

@app.route('/account')
def account():
    return send_from_directory('.', 'account.html')

@app.route('/account.html')
def account_html():
    return send_from_directory('.', 'account.html')

@app.route('/compound-interest-calculator.html')
def compound_interest_calculator():
    return send_from_directory('.', 'compound-interest-calculator.html')


@app.route('/retirement-calculator')
def retirement_calculator():
    return send_from_directory('.', 'retirement-calculator.html')

@app.route('/retirement-calculator.html')
def retirement_calculator_html():
    return send_from_directory('.', 'retirement-calculator.html')

@app.route('/privacy-analyzer')
def privacy_analyzer():
    return send_from_directory('.', 'privacy-analyzer.html')

@app.route('/privacy-analyzer.html')
def privacy_analyzer_html():
    return send_from_directory('.', 'privacy-analyzer.html')

@app.route('/dca-calculator')
def dca_calculator():
    return send_from_directory('.', 'dca-calculator.html')

@app.route('/dca-calculator.html')
def dca_calculator_html():
    return send_from_directory('.', 'dca-calculator.html')


@app.route('/fear-greed-dca')
def fear_greed_dca():
    return send_from_directory('.', 'fear-greed-dca.html')

@app.route('/fear-greed-dca.html')
def fear_greed_dca_html():
    return send_from_directory('.', 'fear-greed-dca.html')


# Initialize database
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

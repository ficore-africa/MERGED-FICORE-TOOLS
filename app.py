from flask import Flask, render_template, request, flash, redirect, url_for, session, send_from_directory
from flask.sessions import SessionInterface, SecureCookieSession
from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, SelectField, BooleanField, SubmitField, RadioField
from wtforms.validators import DataRequired, Email, Optional, ValidationError, NumberRange
from flask_session import Session
from itsdangerous import URLSafeTimedSerializer
from flask_caching import Cache
import numpy as np  # Add at top of app.py
from flask_mail import Mail, Message
import os
import time
import stat  # Added import for stat module
import logging
import json
import threading
import re
import zlib
from datetime import datetime
import pandas as pd
import plotly.express as px
import gspread
from google.oauth2.service_account import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv
import random
from translations import get_translations

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('app.log')]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY')
app.config['DEBUG'] = False  # Set to True during development
mail = Mail(app)
if not app.config['SECRET_KEY']:
    logger.critical("FLASK_SECRET_KEY not set.")
    raise RuntimeError("FLASK_SECRET_KEY not set.")

# Enable zip filter in Jinja2
app.jinja_env.filters['zip'] = lambda *args, **kwargs: zip(*args, **kwargs)
def enumerate_filter(sequence, start=0):
    return enumerate(sequence, start)

app.jinja_env.filters['enumerate'] = enumerate_filter

# Validate environment variables
required_env_vars = ['SMTP_SERVER', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASSWORD', 'SPREADSHEET_ID', 'GOOGLE_CREDENTIALS_JSON']
for var in required_env_vars:
    if not os.getenv(var):
        logger.critical(f"{var} not set.")
        raise RuntimeError(f"{var} not set.")

# Configure SMTP for email
app.config['MAIL_SERVER'] = os.getenv('SMTP_SERVER')
app.config['MAIL_PORT'] = int(os.getenv('SMTP_PORT'))
app.config['MAIL_USERNAME'] = os.getenv('SMTP_USER')
app.config['MAIL_PASSWORD'] = os.getenv('SMTP_PASSWORD')
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False

# Define session directories
SESSION_FILE_DIR = os.path.join(app.root_path, 'flask_session')
SESSION_BACKUP_DIR = os.path.join(app.root_path, 'session_backup')

# Configure server-side session
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(app.root_path, 'flask_session')
app.config['SESSION_PERMANENT'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = 3600
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_COOKIE_NAME'] = 'session_id'
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True in production
os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)

# Create and verify SESSION_FILE_DIR
try:
    os.makedirs(SESSION_FILE_DIR, exist_ok=True)
    # Set directory permissions to 700 (read/write/execute for owner only)
    os.chmod(SESSION_FILE_DIR, stat.S_IRWXU)
    # Verify writability
    test_file = os.path.join(SESSION_FILE_DIR, 'test_write')
    with open(test_file, 'w') as f:
        f.write('test')
    os.remove(test_file)
    logger.info(f"SESSION_FILE_DIR {SESSION_FILE_DIR} created and is writable")
except PermissionError:
    logger.critical(f"Permission denied: Cannot write to {SESSION_FILE_DIR}")
    raise RuntimeError(f"Cannot write to {SESSION_FILE_DIR}")
except Exception as e:
    logger.critical(f"Failed to create or verify {SESSION_FILE_DIR}: {e}")
    raise RuntimeError(f"Failed to create or verify {SESSION_FILE_DIR}")

# Session backup directory
try:
    os.makedirs(SESSION_BACKUP_DIR, exist_ok=True)
    os.chmod(SESSION_BACKUP_DIR, stat.S_IRWXU)
    test_file = os.path.join(SESSION_BACKUP_DIR, 'test_write')
    with open(test_file, 'w') as f:
        f.write('test')
    os.remove(test_file)
    logger.info(f"SESSION_BACKUP_DIR {SESSION_BACKUP_DIR} created and is writable")
except PermissionError:
    logger.critical(f"Permission denied: Cannot write to {SESSION_BACKUP_DIR}")
    raise RuntimeError(f"Cannot write to {SESSION_BACKUP_DIR}")
except Exception as e:
    logger.critical(f"Failed to create or verify {SESSION_BACKUP_DIR}: {e}")
    raise RuntimeError(f"Failed to create or verify {SESSION_BACKUP_DIR}")

# Define sanitize_input for backup filenames
def sanitize_input(email):
    return re.sub(r'[^\w\-_\. ]', '_', email)
    
# Custom session interface for compression
class CompressedSession(SessionInterface):
    def open_session(self, app, request):
        session_data = request.cookies.get(self.get_cookie_name(app))
        if not session_data:
            logger.info("No session cookie found, creating new session")
            return SecureCookieSession()  # Return a new SecureCookieSession
        try:
            serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
            decompressed_data = serializer.loads(session_data)
            compressed_data = bytes.fromhex(decompressed_data)
            session_dict = json.loads(zlib.decompress(compressed_data).decode('utf-8'))
            session = SecureCookieSession(session_dict)  # Wrap dict in SecureCookieSession
            if ('budget_data' not in session and 'health_data' not in session and 'quiz_results' not in session) and session.get('email'):
                session = self.restore_from_backup(session.get('email'), session)
            return session
        except Exception as e:
            logger.error(f"Error decompressing session data: {e}")
            return SecureCookieSession()  # Return empty session on error

    def save_session(self, app, session, response):
        if not session.modified:
            return
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        if not session:
            if self.get_cookie_name(app) in request.cookies:
                response.delete_cookie(self.get_cookie_name(app), domain=domain, path=path)
            return
        try:
            serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
            session_data = json.dumps(dict(session)).encode('utf-8')
            compressed_data = zlib.compress(session_data)
            encoded_data = compressed_data.hex()
            signed_data = serializer.dumps(encoded_data)
            response.set_cookie(
                self.get_cookie_name(app),
                signed_data,
                max_age=app.permanent_session_lifetime,
                secure=app.config['SESSION_COOKIE_SECURE'],
                httponly=True,
                samesite='Lax',
                domain=domain,
                path=path
            )
            if session.get('budget_data', {}).get('email') or session.get('health_data', {}).get('email') or session.get('quiz_results', {}).get('email'):
                email = (session.get('budget_data', {}).get('email') or 
                         session.get('health_data', {}).get('email') or 
                         session.get('quiz_results', {}).get('email'))
                self.backup_session(email, session)
        except Exception as e:
            logger.error(f"Error saving session: {e}")

    def backup_session(self, email, session):
        try:
            backup_file = os.path.join(SESSION_BACKUP_DIR, f"{sanitize_input(email)}.json")
            with open(backup_file, 'w') as f:
                json.dump(dict(session), f)
            logger.info(f"Session backed up for {email}")
        except Exception as e:
            logger.error(f"Failed to backup session for {email}: {e}")

    def restore_from_backup(self, email, session):
        try:
            backup_file = os.path.join(SESSION_BACKUP_DIR, f"{sanitize_input(email)}.json")
            if os.path.exists(backup_file):
                with open(backup_file, 'r') as f:
                    backup_data = json.load(f)
                session.update(backup_data)
                session.modified = True  # Mark session as modified
                logger.info(f"Session restored for {email}")
            return session
        except Exception as e:
            logger.error(f"Failed to restore session for {email}: {e}")
            return session

    def get_cookie_name(self, app):
        return app.config.get('SESSION_COOKIE_NAME', 'session')

    def get_cookie_domain(self, app):
        return app.config.get('SESSION_COOKIE_DOMAIN', None)

    def get_cookie_path(self, app):
        return app.config.get('SESSION_COOKIE_PATH', '/')
        
app.session_interface = CompressedSession()

# Configure caching
app.config['CACHE_TYPE'] = 'filesystem'
app.config['CACHE_DIR'] = os.path.join(app.root_path, 'cache')
app.config['CACHE_DEFAULT_TIMEOUT'] = 3600
os.makedirs(app.config['CACHE_DIR'], exist_ok=True)
cache = Cache(app)

# Custom validator
def non_negative(form, field):
    if field.data < 0:
        raise ValidationError('Value must be non-negative.')

# Currency formatting filter
def format_currency(value, currency='NGN'):
    try:
        formatted = f"{float(value):,.2f}"
        return f"₦{formatted}" if currency == 'NGN' else f"{currency} {formatted}"
    except (ValueError, TypeError):
        logger.error(f"Invalid value for format_currency: {value}")
        return str(value)
app.jinja_env.filters['format_currency'] = format_currency

# Google Sheets setup
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
sheets = None
sheets_lock = threading.Lock()

# URL constants
FEEDBACK_FORM_URL = os.getenv('FEEDBACK_FORM_URL', 'https://docs.google.com/forms/feedback')
WAITLIST_FORM_URL = os.getenv('WAITLIST_FORM_URL', 'https://docs.google.com/forms/waitlist')
CONSULTANCY_FORM_URL = os.getenv('CONSULTANCY_FORM_URL', 'https://docs.google.com/forms/consultancy')
LINKEDIN_URL = os.getenv('LINKEDIN_URL', 'https://www.linkedin.com/in/ficore-africa')
TWITTER_URL = os.getenv('TWITTER_URL', 'https://x.com/Ficore_Africa')
FACEBOOK_URL = os.getenv('FACEBOOK_URL', 'https://www.facebook.com/profile.php?id=61575627944628&mibextid=ZbWKwL')
INVESTING_COURSE_URL = 'https://youtube.com/@ficore.africa'
SAVINGS_COURSE_URL = 'https://www.youtube.com/@FICORE.AFRICA'
DEBT_COURSE_URL = 'https://www.youtube.com/@FICORE.AFRICA'
RECOVERY_COURSE_URL = 'https://www.youtube.com/@FICORE.AFRICA'

# Headers for Google Sheets
PREDETERMINED_HEADERS_BUDGET = [
    'Timestamp', 'first_name', 'email', 'language', 'monthly_income',
    'housing_expenses', 'food_expenses', 'transport_expenses', 'other_expenses',
    'savings_goal', 'auto_email', 'total_expenses', 'savings', 'surplus_deficit',
    'badges', 'rank', 'total_users'
]
PREDETERMINED_HEADERS_HEALTH = [
    'Timestamp', 'business_name', 'income_revenue', 'expenses_costs', 'debt_loan',
    'debt_interest_rate', 'auto_email', 'phone_number', 'first_name', 'last_name',
    'user_type', 'email', 'badges', 'language'
]
PREDETERMINED_HEADERS_QUIZ = [
    'Timestamp', 'first_name', 'email', 'language',
    *(f'question_{i}' for i in range(1, 11)),
    *(f'answer_{i}' for i in range(1, 11)),
    'personality', 'badges', 'auto_email'
]

def sanitize_input(text):
    if not text:
        return text
    return re.sub(r'[<>";]', '', text.strip())[:100]

def get_sheets_client():
    global sheets
    if sheets is None:
        logger.error("Google Sheets client not initialized.")
        return None
    return sheets

def set_sheet_headers(headers, worksheet_name):
    try:
        client = get_sheets_client()
        if client is None:
            return False
        try:
            worksheet = client.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            client.add_worksheet(worksheet_name, rows=100, cols=len(headers))
            worksheet = client.worksheet(worksheet_name)
        
        col_num = len(headers)
        col_letter = ''
        while col_num > 0:
            col_num, remainder = divmod(col_num - 1, 26)
            col_letter = chr(65 + remainder) + col_letter
        range_end = col_letter + '1'
        worksheet.update(f'A1:{range_end}', [headers])
        logger.info(f"Headers set in worksheet '{worksheet_name}'.")
        return True
    except Exception as e:
        logger.error(f"Error setting headers in '{worksheet_name}': {e}")
        return False
        
def initialize_sheets(max_retries=5, backoff_factor=2):
    global sheets
    if not SPREADSHEET_ID:
        logger.critical("SPREADSHEET_ID not set.")
        return False
    for attempt in range(max_retries):
        try:
            creds_dict = json.loads(os.getenv('GOOGLE_CREDENTIALS_JSON'))
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPE)
            client = gspread.authorize(creds)
            sheets = client.open_by_key(SPREADSHEET_ID)
            set_sheet_headers(PREDETERMINED_HEADERS_BUDGET, 'Budget')
            set_sheet_headers(PREDETERMINED_HEADERS_HEALTH, 'Health')
            set_sheet_headers(PREDETERMINED_HEADERS_QUIZ, 'Quiz')
            logger.info("Google Sheets initialized.")
            return True
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)
    logger.critical("Failed to initialize Google Sheets.")
    return False

if not initialize_sheets():
    raise RuntimeError("Failed to initialize Google Sheets.")

@cache.memoize(timeout=3600)
def fetch_data_from_sheet(email=None, headers=PREDETERMINED_HEADERS_HEALTH, worksheet_name='Health'):
    try:
        client = get_sheets_client()
        if client is None:
            return pd.DataFrame(columns=headers)
        try:
            worksheet = client.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            client.add_worksheet(worksheet_name, rows=100, cols=len(headers))
            worksheet = client.worksheet(worksheet_name)
            worksheet.update('A1:' + chr(64 + len(headers)) + '1', [headers])
        values = worksheet.get_all_values()
        if not values:
            return pd.DataFrame(columns=headers)
        rows = values[1:] if len(values) > 1 else []
        adjusted_rows = [row + [''] * (len(headers) - len(row)) if len(row) < len(headers) else row[:len(headers)] for row in rows]
        df = pd.DataFrame(adjusted_rows, columns=headers)
        df['language'] = df['language'].replace('', 'en')
        if email:
            df = df[df['email'] == email].head(1) if headers == PREDETERMINED_HEADERS_BUDGET else df[df['email'] == email]
        logger.info(f"Fetched {len(df)} rows from '{worksheet_name}'.")
        return df
    except Exception as e:
        logger.error(f"Error fetching data from '{worksheet_name}': {e}")
        return pd.DataFrame(columns=headers)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def append_to_sheet(data, headers, worksheet_name='Health'):
    try:
        if len(data) != len(headers):
            logger.error(f"Invalid data length for '{worksheet_name}': {data}")
            return False
        client = get_sheets_client()
        if client is None:
            return False
        try:
            worksheet = client.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            client.add_worksheet(worksheet_name, rows=100, cols=len(headers))
            worksheet = client.worksheet(worksheet_name)
            worksheet.update('A1:' + chr(64 + len(headers)) + '1', [headers])
        worksheet.append_row(data, value_input_option='RAW')
        logger.info(f"Appended data to '{worksheet_name}'.")
        time.sleep(1)
        return True
    except Exception as e:
        logger.error(f"Error appending to '{worksheet_name}': {e}")
        return False

def calculate_budget_metrics(df):
    try:
        if df.empty:
            logger.warning("Empty DataFrame in calculate_budget_metrics.")
            return df
        for col in ['monthly_income', 'housing_expenses', 'food_expenses', 'transport_expenses', 'other_expenses', 'savings_goal']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
        df['total_expenses'] = df['housing_expenses'] + df['food_expenses'] + df['transport_expenses'] + df['other_expenses']
        df['savings'] = df.apply(
            lambda row: max(0, row['monthly_income'] * 0.1) if pd.isna(row['savings_goal']) or row['savings_goal'] == 0 else row['savings_goal'],
            axis=1
        )
        df['surplus_deficit'] = df['monthly_income'] - df['total_expenses'] - df['savings']
        df['outcome_status'] = df['surplus_deficit'].apply(lambda x: 'Savings' if x >= 0 else 'Overspend')
        df['advice'] = df['surplus_deficit'].apply(
            lambda x: get_translations(df['language'].iloc[0])['Great job! Save or invest your surplus to grow your wealth.'] if x >= 0 
            else get_translations(df['language'].iloc[0])['Reduce non-essential spending to balance your budget.']
        )
        return df
    except Exception as e:
        logger.error(f"Error in calculate_budget_metrics: {e}")
        return df

def assign_badges_budget(user_df):
    badges = []
    try:
        if user_df.empty:
            logger.warning("Empty user_df in assign_badges_budget.")
            return badges
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        language = user_df.iloc[0].get('language', 'en')
        if len(user_df) == 1:
            badges.append(get_translations(language)['First Budget Completed!'])
        return badges
    except Exception as e:
        logger.error(f"Error in assign_badges_budget: {e}")
        return badges

def calculate_health_score(df):
    try:
        if df.empty:
            logger.warning("Empty DataFrame in calculate_health_score.")
            df['HealthScore'] = 0.0
            return df
        for col in ['income_revenue', 'expenses_costs', 'debt_loan', 'debt_interest_rate']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
        df['IncomeRevenueSafe'] = df['income_revenue'].replace(0, 1e-10)
        df['CashFlowRatio'] = (df['income_revenue'] - df['expenses_costs']) / df['IncomeRevenueSafe']
        df['DebtToIncomeRatio'] = df['debt_loan'] / df['IncomeRevenueSafe']
        df['DebtInterestBurden'] = df['debt_interest_rate'].clip(lower=0) / 20
        df['DebtInterestBurden'] = df['DebtInterestBurden'].clip(upper=1)
        df['NormCashFlow'] = df['CashFlowRatio'].clip(0, 1)
        df['NormDebtToIncome'] = 1 - df['DebtToIncomeRatio'].clip(0, 1)
        df['NormDebtInterest'] = 1 - df['DebtInterestBurden']
        df['HealthScore'] = (
            df['NormCashFlow'] * 0.333 +
            df['NormDebtToIncome'] * 0.333 +
            df['NormDebtInterest'] * 0.333
        ) * 100
        df['HealthScore'] = df['HealthScore'].round(2)
        df[['ScoreDescription', 'CourseTitle', 'CourseURL']] = df.apply(
            score_description_and_course, axis=1, result_type='expand'
        )
        return df
    except Exception as e:
        logger.error(f"Error calculating health score: {e}")
        df['HealthScore'] = 0.0
        return df

def score_description_and_course(row):
    score = row['HealthScore']
    cash_flow = row['CashFlowRatio']
    debt_to_income = row['DebtToIncomeRatio']
    debt_interest = row['DebtInterestBurden']
    clean_urls = {
        'investing': INVESTING_COURSE_URL.split('?')[0],
        'debt': DEBT_COURSE_URL.split('?')[0],
        'savings': SAVINGS_COURSE_URL.split('?')[0],
        'recovery': RECOVERY_COURSE_URL.split('?')[0]
    }
    if score >= 75:
        return ('Stable Income; invest excess now', 'Ficore Simplified Investing Course', clean_urls['investing'])
    elif score >= 50:
        if cash_flow < 0.3 or debt_interest > 0.5:
            return ('At Risk; manage your expense!', 'Ficore Debt and Expense Management', clean_urls['debt'])
        return ('Moderate; save something monthly!', 'Ficore Savings Mastery', clean_urls['savings'])
    elif score >= 25:
        return ('At Risk; manage your expense!', 'Ficore Debt and Expense Management', clean_urls['debt'])
    else:
        return ('Critical; seek financial help!', 'Ficore Financial Recovery', clean_urls['recovery'])

def assign_badges_health(user_df, all_users_df):
    badges = []
    if user_df.empty:
        logger.warning("Empty user_df in assign_badges_health.")
        return badges
    try:
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', dayfirst=True, errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_row = user_df.iloc[0]
        language = user_row['language']
        if len(user_df) == 1:
            badges.append(get_translations(language)['First Health Score Completed!'])
        if user_row['HealthScore'] >= 50:
            badges.append(get_translations(language)['Financial Stability Achieved!'])
        if user_row['DebtToIncomeRatio'] < 0.3:
            badges.append(get_translations(language)['Debt Slayer!'])
        return badges
    except Exception as e:
        logger.error(f"Error in assign_badges_health: {e}")
        return badges

def send_health_email(to_email, user_name, health_score, score_description, rank, total_users, course_title, course_url, language):
    try:
        trans = get_translations(language)
        subject = trans['Top 10% Subject'] if rank <= total_users * 0.1 else trans['Score Report Subject']
        msg = Message(
            subject=subject,
            recipients=[to_email],
            html=render_template(
                'health_score_email.html',
                trans=trans,
                user_name=sanitize_input(user_name),
                health_score=health_score,
                score_description=score_description,
                rank=rank,
                total_users=total_users,
                course_title=course_title,
                course_url=course_url,
                FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
                WAITLIST_FORM_URL=WAITLIST_FORM_URL,
                CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
                LINKEDIN_URL=LINKEDIN_URL,
                TWITTER_URL=TWITTER_URL,
                language=language
            )
        )
        mail.send(msg)
        logger.info(f"Health email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Error sending health email to {to_email}: {e}")
        return False

def send_health_email_async(to_email, user_name, health_score, score_description, rank, total_users, course_title, course_url, language):
    with app.app_context():
        send_health_email(to_email, user_name, health_score, score_description, rank, total_users, course_title, course_url, language)

def generate_breakdown_plot(user_df):
    try:
        if user_df.empty:
            return None
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', dayfirst=True, errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_row = user_df.iloc[0]
        labels = ['Cash Flow', 'Debt-to-Income', 'Debt Interest']
        values = [
            user_row['NormCashFlow'] * 100 / 3,
            user_row['NormDebtToIncome'] * 100 / 3,
            user_row['NormDebtInterest'] * 100 / 3
        ]
        fig = px.bar(x=labels, y=values, title='Score Breakdown', labels={'x': 'Component', 'y': 'Score Contribution'})
        fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), height=300, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
        return fig.to_html(full_html=False, include_plotlyjs=False)
    except Exception as e:
        logger.error(f"Error generating breakdown plot: {e}")
        return None

def generate_comparison_plot(user_df, all_users_df):
    try:
        if user_df.empty or all_users_df.empty:
            return None
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', dayfirst=True, errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_score = user_df.iloc[0]['HealthScore']
        avg_score = all_users_df['HealthScore'].astype(float).mean()
        fig = px.bar(
            x=['Your Score', 'Average Peer Score'],
            y=[user_score, avg_score],
            title='How Your Score Compares',
            labels={'x': 'Score Type', 'y': 'Score'}
        )
        fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), height=300, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
        return fig.to_html(full_html=False, include_plotlyjs=False)
    except Exception as e:
        logger.error(f"Error generating comparison plot: {e}")
        return None

# Form definitions
class Step1Form(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('en', 'English'), ('ha', 'Hausa')], default='en')
    submit = SubmitField()
    def __init__(self, language='en', *args, **kwargs):
        super(Step1Form, self).__init__(*args, **kwargs)
        self.submit.label.text = get_translations(language)['Continue to Income']

class Step2Form(FlaskForm):
    monthly_income = FloatField('Monthly Income', validators=[DataRequired(), non_negative])
    submit = SubmitField()
    back = SubmitField('Back')
    def __init__(self, language='en', *args, **kwargs):
        super(Step2Form, self).__init__(*args, **kwargs)
        self.submit.label.text = get_translations(language)['Continue to Expenses']

class Step3Form(FlaskForm):
    housing_expenses = FloatField('Housing Expenses', validators=[DataRequired(), non_negative])
    food_expenses = FloatField('Food Expenses', validators=[DataRequired(), non_negative])
    transport_expenses = FloatField('Transport Expenses', validators=[DataRequired(), non_negative])
    other_expenses = FloatField('Other Expenses', validators=[DataRequired(), non_negative])
    submit = SubmitField()
    back = SubmitField('Back')
    def __init__(self, language='en', *args, **kwargs):
        super(Step3Form, self).__init__(*args, **kwargs)
        self.submit.label.text = get_translations(language)['Continue to Savings & Review']

class Step4Form(FlaskForm):
    savings_goal = FloatField('Savings Goal', validators=[Optional(), non_negative])
    auto_email = BooleanField('Receive Email Report')
    submit = SubmitField()
    back = SubmitField('Back')
    def __init__(self, language='en', *args, **kwargs):
        super(Step4Form, self).__init__(*args, **kwargs)
        self.submit.label.text = get_translations(language)['Continue to Dashboard']

class HealthScoreStep1Form(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('en', 'English'), ('ha', 'Hausa')], default='en', validators=[DataRequired()])
    auto_email = BooleanField('Receive Email Report')
    submit = SubmitField()

    def __init__(self, language=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        trans = get_translations(language or session.get('language', 'en'))
        self.submit.label.text = trans.get('Next', 'Next')
        if not self.language.data:
            self.language.data = language or session.get('language', 'en')

class HealthScoreStep2Form(FlaskForm):
    business_name = StringField('Business Name', validators=[DataRequired()])
    user_type = SelectField('User Type', choices=[('SME', 'SME'), ('Individual', 'Individual')], validators=[DataRequired()])
    submit = SubmitField()

    def __init__(self, language=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        trans = get_translations(language or session.get('language', 'en'))
        self.submit.label.text = trans.get('Next', 'Next')

class HealthScoreStep3Form(FlaskForm):
    income_revenue = FloatField('Monthly Income/Revenue', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    expenses_costs = FloatField('Monthly Expenses/Costs', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    debt_loan = FloatField('Total Debt/Loan Amount', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    debt_interest_rate = FloatField('Debt Interest Rate (%)', validators=[DataRequired(), NumberRange(min=0, max=100)])
    submit = SubmitField()

    def __init__(self, language=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        trans = get_translations(language or session.get('language', 'en'))
        self.submit.label.text = trans.get('Submit', 'Submit')

# Load Quiz Questions
try:
    with open('questions.json', 'r', encoding='utf-8') as f:
        QUIZ_QUESTIONS = json.load(f)
    logger.debug(f"Successfully loaded QUIZ_QUESTIONS: {QUIZ_QUESTIONS}")
except FileNotFoundError:
    logger.error("questions.json file not found. Ensure it exists in the project root directory.")
    QUIZ_QUESTIONS = []
except json.JSONDecodeError as e:
    logger.error(f"Error decoding questions.json: {e}")
    QUIZ_QUESTIONS = []

class QuizForm(FlaskForm):
    first_name = StringField('First Name', validators=[Optional()])
    email = StringField('Email', validators=[Optional(), Email()])
    language = SelectField('Language', choices=[('en', 'English'), ('ha', 'Hausa')], default='en')
    auto_email = BooleanField('Receive Email Report')
    submit = SubmitField('Next')
    back = SubmitField('Back')

    def __init__(self, questions=None, language='en', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.trans = get_translations(language)
        self.questions = questions or []
        logger.debug(f"Initializing QuizForm with questions: {[q['id'] for q in self.questions]}")

        # Dynamically add question fields with translated labels and options
        for i, q in enumerate(self.questions, 1):
            field_name = f'question_{i}'
            # Translate question text and options
            translated_text = self.trans.get(q['text'], q['text'])
            translated_options = [(opt, self.trans.get(opt, opt)) for opt in q['options']]
            # Create a bound field instance
            field = RadioField(
                translated_text,
                validators=[DataRequired() if q.get('required', True) else Optional()],
                choices=translated_options,
                id=field_name
            )
            # Bind the field to the form instance
            bound_field = field.bind(self, field_name)
            self._fields[field_name] = bound_field
            logger.debug(f"Added field {field_name} with translated text '{translated_text}' and options {translated_options}")

        # Update labels with translations
        self.first_name.label.text = self.trans.get('First Name', 'First Name')
        self.email.label.text = self.trans.get('Email', 'Email')
        self.language.label.text = self.trans.get('Language', 'Language')
        self.auto_email.label.text = self.trans.get('Receive Email Report', 'Receive Email Report')
        self.submit.label.text = self.trans.get('Next', 'Next')
        self.back.label.text = self.trans.get('Previous', 'Previous')

    def validate(self, extra_validators=None):
        logger.debug(f"Validating QuizForm with fields: {list(self._fields.keys())}")
        rv = super().validate(extra_validators)
        if not rv:
            logger.error(f"Validation failed with errors: {self.errors}")
        return rv
        
# Personality, Badges, Chart, and Email Functions
def assign_personality(answers, language='en'):
    trans = get_translations(language)
    score = 0
    for q, a in answers:
        weight = q.get('weight', 1)
        # Translate positive and negative answers based on the current language
        positive = [trans.get(opt, opt) for opt in q.get('positive_answers', ['yes'])]
        negative = [trans.get(opt, opt) for opt in q.get('negative_answers', ['no'])]
        if a in positive:
            score += weight
        elif a in negative:
            score -= weight
    if score >= 6:
        return 'Planner', trans.get('Planner', 'You plan your finances well.'), trans.get('Planner Tip', 'Save regularly.')
    elif score >= 2:
        return 'Saver', trans.get('Saver', 'You save consistently.'), trans.get('Saver Tip', 'Increase your savings rate.')
    elif score >= 0:
        return 'Minimalist', trans.get('Minimalist', 'You maintain a balanced approach.'), trans.get('Minimalist Tip', 'Consider a budget.')
    elif score >= -2:
        return 'Spender', trans.get('Spender', 'You enjoy spending.'), trans.get('Spender Tip', 'Track your expenses.')
    else:
        return 'Avoider', trans.get('Avoider', 'You avoid financial planning.'), trans.get('Avoider Tip', 'Start with a simple plan.')
        
def assign_badges_quiz(user_df, all_users_df):
    badges = []
    if user_df.empty:
        logger.warning("Empty user_df in assign_badges_quiz.")
        return badges
    try:
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_row = user_df.iloc[0]
        language = user_row.get('language', 'en')
        trans = get_translations(language)
        if len(user_df) >= 1:
            badges.append(trans.get('First Quiz Completed!', 'First Quiz Completed!'))
        if user_row['personality'] == 'Planner':
            badges.append(trans.get('Master Planner!', 'Master Planner!'))
        elif user_row['personality'] == 'Avoider' and len(all_users_df) > 10:
            badges.append(trans.get('Needs Guidance!', 'Needs Guidance!'))
        return badges
    except Exception as e:
        logger.error(f"Error in assign_badges_quiz: {e}")
        return badges

def generate_quiz_summary_chart(answers, language='en'):
    try:
        answer_counts = {}
        for _, answer in answers:
            answer_counts[answer] = answer_counts.get(answer, 0) + 1
        labels = list(answer_counts.keys())
        values = list(answer_counts.values())
        trans = get_translations(language)
        fig = px.bar(
            x=labels,
            y=values,
            title=trans.get('Quiz Summary', 'Quiz Summary'),
            labels={'x': trans.get('Answer', 'Answer'), 'y': trans.get('Count', 'Count')}
        )
        fig.update_layout(
            margin=dict(l=20, r=20, t=30, b=20),
            height=300,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)'
        )
        return fig.to_html(full_html=False, include_plotlyjs=False)
    except Exception as e:
        logger.error(f"Error generating quiz summary chart: {e}")
        return None

def send_quiz_email(to_email, user_name, personality, personality_desc, tip, language):
    try:
        trans = get_translations(language)
        msg = Message(
            subject=trans.get('Quiz Report Subject', 'Your Quiz Report'),
            recipients=[to_email],
            html=render_template(
                'quiz_email.html',
                trans=trans,
                user_name=sanitize_input(user_name) or 'User',
                personality=personality,
                personality_desc=personality_desc,
                tip=tip,
                FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
                WAITLIST_FORM_URL=WAITLIST_FORM_URL,
                CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
                LINKEDIN_URL=LINKEDIN_URL,
                TWITTER_URL=TWITTER_URL,
                language=language
            )
        )
        mail.send(msg)
        logger.info(f"Quiz email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Error sending quiz email to {to_email}: {e}")
        return False

def send_quiz_email_async(to_email, user_name, personality, personality_desc, tip, language):
    with app.app_context():
        send_quiz_email(to_email, user_name, personality, personality_desc, tip, language)

def send_budget_email(to_email, user_name, user_data, language):
    try:
        trans = get_translations(language)
        msg = Message(
            subject=trans.get('Budget Report Subject', 'Your Budget Report'),
            recipients=[to_email],
            html=render_template(
                'budget_email.html',
                trans=trans,
                user_name=sanitize_input(user_name),
                user_data=user_data,
                FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
                WAITLIST_FORM_URL=WAITLIST_FORM_URL,
                CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
                LINKEDIN_URL=LINKEDIN_URL,
                TWITTER_URL=TWITTER_URL,
                language=language
            )
        )
        mail.send(msg)
        logger.info(f"Budget email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Error sending budget email to {to_email}: {e}")
        return False

def send_budget_email_async(to_email, user_name, user_data, language):
    with app.app_context():
        send_budget_email(to_email, user_name, user_data, language)

# Routes
@app.route('/change_language', methods=['POST'])
def change_language():
    language = request.form.get('language', 'en')
    if language in ['en', 'ha']:
        session['language'] = language
        session.modified = True
    return redirect(request.args.get('next') or url_for('index'))

@app.route('/', methods=['GET', 'POST'])
def index():
    language = request.args.get('language', session.get('language', 'en'))
    session['language'] = language
    session.modified = True
    tool = request.args.get('tool', 'budget')
    return render_template(
        'index.html',
        trans=get_translations(language),
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        tool=tool,
        language=language
    )

@app.route('/budget_step1', methods=['GET', 'POST'])
def budget_step1():
    language = session.get('language', 'en')
    form = Step1Form(language=language)
    trans = get_translations(language)
    if form.validate_on_submit():
        session['budget_data'] = {
            'first_name': sanitize_input(form.first_name.data),
            'email': sanitize_input(form.email.data),
            'language': form.language.data
        }
        session['language'] = form.language.data
        session.modified = True
        return redirect(url_for('budget_step2'))
    return render_template(
        'budget_step1.html',
        form=form,
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        step=1,
        language=language
    )

@app.route('/budget_step2', methods=['GET', 'POST'])
def budget_step2():
    language = session.get('language', 'en')
    trans = get_translations(language)
    form = Step2Form(language=language)
    if 'budget_data' not in session:
        flash(trans['Session Expired'], 'error')
        return redirect(url_for('budget_step1'))
    if form.validate_on_submit():
        if form.back.data:
            return redirect(url_for('budget_step1'))
        try:
            monthly_income = float(request.form.get('monthly_income', '0').replace(',', ''))
            session['budget_data']['monthly_income'] = monthly_income
            session.modified = True
            return redirect(url_for('budget_step3'))
        except ValueError:
            flash(trans['Invalid Number'], 'error')
    return render_template(
        'budget_step2.html',
        form=form,
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        step=2,
        language=language
    )

@app.route('/budget_step3', methods=['GET', 'POST'])
def budget_step3():
    language = session.get('language', 'en')
    trans = get_translations(language)
    form = Step3Form(language=language)
    if 'budget_data' not in session:
        flash(trans['Session Expired'], 'error')
        return redirect(url_for('budget_step1'))
    if form.validate_on_submit():
        if form.back.data:
            return redirect(url_for('budget_step2'))
        try:
            session['budget_data'].update({
                'housing_expenses': float(request.form.get('housing_expenses', '0').replace(',', '')),
                'food_expenses': float(request.form.get('food_expenses', '0').replace(',', '')),
                'transport_expenses': float(request.form.get('transport_expenses', '0').replace(',', '')),
                'other_expenses': float(request.form.get('other_expenses', '0').replace(',', ''))
            })
            session.modified = True
            return redirect(url_for('budget_step4'))
        except ValueError:
            logger.error(f"Invalid number input in budget_step3")
            flash(trans['Invalid Number'], 'error')
    return render_template(
        'budget_step3.html',
        form=form,
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        step=3,
        language=language
    )

@app.route('/budget_step4', methods=['GET', 'POST'])
def budget_step4():
    language = session.get('language', 'en')
    trans = get_translations(language)
    form = Step4Form(language=language)
    if 'budget_data' not in session:
        flash(trans['Session Expired'], 'error')
        return redirect(url_for('budget_step1'))
    if form.validate_on_submit():
        if form.back.data:
            return redirect(url_for('budget_step3'))
        try:
            savings_goal = float(request.form.get('savings_goal', '0').replace(',', '')) if request.form.get('savings_goal') else 0.0
            session['budget_data'].update({
                'savings_goal': savings_goal,
                'auto_email': form.auto_email.data
            })
            session.modified = True
            budget_data = session['budget_data']
            df = pd.DataFrame([budget_data], columns=PREDETERMINED_HEADERS_BUDGET)
            df = calculate_budget_metrics(df)
            if df.empty:
                flash(trans['Error retrieving data. Please try again.'], 'error')
                return redirect(url_for('budget_step1'))
            user_df = df
            badges = assign_badges_budget(user_df)
            user_df['badges'] = ', '.join(badges)
            data = [
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                budget_data.get('first_name', ''),
                budget_data.get('email', ''),
                language,
                budget_data.get('monthly_income', 0),
                budget_data.get('housing_expenses', 0),
                budget_data.get('food_expenses', 0),
                budget_data.get('transport_expenses', 0),
                budget_data.get('other_expenses', 0),
                budget_data.get('savings_goal', 0),
                str(budget_data.get('auto_email', False)).lower(),
                user_df['total_expenses'].iloc[0],
                user_df['savings'].iloc[0],
                user_df['surplus_deficit'].iloc[0],
                user_df['badges'].iloc[0],
                0,
                0
            ]
            if not append_to_sheet(data, PREDETERMINED_HEADERS_BUDGET, 'Budget'):
                flash(trans['Google Sheets Error'], 'error')
                return redirect(url_for('budget_step1'))
            if budget_data.get('auto_email'):
                threading.Thread(
                    target=send_budget_email_async,
                    args=(budget_data['email'], budget_data['first_name'], user_df.iloc[0], language)
                ).start()
                flash(trans['Check Inbox'], 'success')
            flash(trans['Submission Success'], 'success')
            return redirect(url_for('budget_dashboard'))
        except ValueError:
            logger.error(f"Invalid number input in budget_step4")
            flash(trans['Invalid Number'], 'error')
        except Exception as e:
            logger.error(f"Error in budget_step4: {e}")
            flash(trans['Error retrieving data. Please try again.'], 'error')
            return redirect(url_for('budget_step1'))
    return render_template(
        'budget_step4.html',
        form=form,
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        step=4,
        language=language
    )

@app.route('/budget_dashboard', methods=['GET', 'POST'])
def budget_dashboard():
    language = session.get('language', 'en')
    trans = get_translations(language)
    if 'budget_data' not in session or not session['budget_data'].get('email'):
        flash(trans['Session Expired'], 'error')
        return redirect(url_for('budget_step1'))
    email = session['budget_data']['email']
    try:
        if 'budget_data' in session:
            budget_data = session['budget_data']
            df = pd.DataFrame([budget_data], columns=PREDETERMINED_HEADERS_BUDGET)
            user_df = calculate_budget_metrics(df)
            user_df['Timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        else:
            user_df = fetch_data_from_sheet(email=email, headers=PREDETERMINED_HEADERS_BUDGET, worksheet_name='Budget')
            if user_df.empty:
                flash(trans['Error retrieving data. Please try again.'], 'error')
                return redirect(url_for('budget_step1'))
            user_df = calculate_budget_metrics(user_df)
            user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', errors='coerce')
        all_users_df = fetch_data_from_sheet(headers=PREDETERMINED_HEADERS_BUDGET, worksheet_name='Budget')
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_row = user_df.iloc[0]
        rank = sum(all_users_df['surplus_deficit'].astype(float) > user_row['surplus_deficit']) + 1
        total_users = len(all_users_df)
        badges = assign_badges_budget(user_df)
        budget_breakdown = {
            'Housing': user_row['housing_expenses'],
            'Food': user_row['food_expenses'],
            'Transport': user_row['transport_expenses'],
            'Other': user_row['other_expenses']
        }
        breakdown_fig = px.pie(names=list(budget_breakdown.keys()), values=list(budget_breakdown.values()), title=trans['Budget Breakdown'])
        breakdown_plot = breakdown_fig.to_html(full_html=False, include_plotlyjs=False)
        comparison_fig = px.bar(x=['Income', 'Expenses', 'Savings'], y=[user_row['monthly_income'], user_row['total_expenses'], user_row['savings']], title=trans['Income vs Expenses'])
        comparison_plot = comparison_fig.to_html(full_html=False, include_plotlyjs=False)
        session.pop('budget_data', None)
        session.modified = True
        return render_template(
            'budget_dashboard.html',
            trans=trans,
            user_data=user_row,
            badges=badges,
            rank=rank,
            total_users=total_users,
            breakdown_plot=breakdown_plot,
            comparison_plot=comparison_plot,
            FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
            WAITLIST_FORM_URL=WAITLIST_FORM_URL,
            CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
            LINKEDIN_URL=LINKEDIN_URL,
            TWITTER_URL=TWITTER_URL,
            FACEBOOK_URL=FACEBOOK_URL,
            language=language
        )
    except Exception as e:
        logger.error(f"Error in budget_dashboard: {e}")
        flash(trans['Error retrieving data. Please try again.'], 'error')
        return redirect(url_for('budget_step1'))

@app.route('/health_score_step1', methods=['GET', 'POST'])
def health_score_step1():
    logger.debug(f"Session before: {session}")
    language = session.get('language', 'en')
    form = HealthScoreStep1Form(language=language)
    trans = get_translations(language)

    if form.validate_on_submit():
        session['health_data'] = {
            'first_name': sanitize_input(form.first_name.data),
            'email': sanitize_input(form.email.data),
            'language': form.language.data,
            'auto_email': form.auto_email.data
        }
        session['language'] = form.language.data
        session.modified = True
        logger.info(f"Health score step 1 validated successfully for email: {form.email.data}")
        return redirect(url_for('health_score_step2'))

    if form.errors:
        for field, errors in form.errors.items():
            for error in errors:
                logger.error(f"Validation error in {field}: {error}")
        flash(trans['Please correct the errors below'], 'error')

    return render_template(
        'health_score.html',
        form=form,
        step=1,
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        language=language
    )

@app.route('/health_score_step2', methods=['GET', 'POST'])
def health_score_step2():
    if 'health_data' not in session:
        flash(get_translations(session.get('language', 'en'))['Session Expired'], 'error')
        return redirect(url_for('health_score_step1'))

    language = session.get('language', 'en')
    form = HealthScoreStep2Form(language=language)
    trans = get_translations(language)

    if form.validate_on_submit():
        session['health_data'].update({
            'business_name': sanitize_input(form.business_name.data),
            'user_type': form.user_type.data
        })
        session.modified = True
        logger.info(f"Health score step 2 validated successfully")
        return redirect(url_for('health_score_step3'))

    if form.errors:
        for field, errors in form.errors.items():
            for error in errors:
                logger.error(f"Validation error in {field}: {error}")
        flash(trans['Please correct the errors below'], 'error')

    return render_template(
        'health_score.html',
        form=form,
        step=2,
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        language=language
    )

@app.route('/health_score_step3', methods=['GET', 'POST'])
def health_score_step3():
    if 'health_data' not in session:
        flash(get_translations(session.get('language', 'en'))['Session Expired'], 'error')
        return redirect(url_for('health_score_step1'))

    language = session.get('language', 'en')
    form = HealthScoreStep3Form(language=language)
    trans = get_translations(language)

    if form.validate_on_submit():
        try:
            income_revenue = float(request.form.get('income_revenue', '0').replace(',', ''))
            expenses_costs = float(request.form.get('expenses_costs', '0').replace(',', ''))
            debt_loan = float(request.form.get('debt_loan', '0').replace(',', ''))
            debt_interest_rate = float(request.form.get('debt_interest_rate', '0').replace(',', ''))

            health_data = session['health_data']
            health_data.update({
                'income_revenue': float(income_revenue),
                'expenses_costs': float(expenses_costs),
                'debt_loan': float(debt_loan),
                'debt_interest_rate': float(debt_interest_rate)
            })
            session['health_data'] = {
                key: float(val) if isinstance(val, (np.int64, np.float64)) else int(val) if isinstance(val, np.int64) else val
                for key, val in health_data.items()
            }
            session.modified = True
            logger.info(f"Health score step 3 validated successfully")

            data = [
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                health_data.get('business_name', ''),
                float(health_data.get('income_revenue', 0.0)),
                float(health_data.get('expenses_costs', 0.0)),
                float(health_data.get('debt_loan', 0.0)),
                float(health_data.get('debt_interest_rate', 0.0)),
                str(health_data.get('auto_email', False)).lower(),
                '',
                health_data.get('first_name', ''),
                '',
                health_data.get('user_type', ''),
                health_data.get('email', ''),
                '',
                health_data.get('language', 'en')
            ]

            if not append_to_sheet(data, PREDETERMINED_HEADERS_HEALTH, 'Health'):
                flash(trans['Google Sheets Error'], 'error')
                return redirect(url_for('health_score_step1'))

            user_df = fetch_data_from_sheet(email=health_data['email'], headers=PREDETERMINED_HEADERS_HEALTH, worksheet_name='Health')
            all_users_df = fetch_data_from_sheet(headers=PREDETERMINED_HEADERS_HEALTH, worksheet_name='Health')
            user_df = calculate_health_score(user_df)
            all_users_df = calculate_health_score(all_users_df)

            if user_df.empty:
                flash(trans['Error retrieving data. Please try again.'], 'error')
                return redirect(url_for('health_score_step1'))

            user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', errors='coerce')
            user_df = user_df.sort_values('Timestamp', ascending=False)
            user_row = user_df.iloc[0]

            badges = assign_badges_health(user_df, all_users_df)
            all_scores = all_users_df['HealthScore'].astype(float).sort_values(ascending=False)
            rank = (all_scores >= user_row['HealthScore']).sum()
            total_users = len(all_scores)

            user_row_dict = {
                key: float(val) if isinstance(val, (np.float64, np.int64)) else
                     int(val) if isinstance(val, np.int64) else
                     val.strftime('%Y-%m-%d %H:%M:%S') if isinstance(val, pd.Timestamp) else
                     val
                for key, val in user_row.to_dict().items()
            }

            session['dashboard_data'] = {
                'first_name': health_data['first_name'],
                'email': health_data['email'],
                'language': health_data['language'],
                'health_score': float(user_row['HealthScore']),
                'score_description': user_row['ScoreDescription'],
                'course_title': user_row['CourseTitle'],
                'course_url': user_row['CourseURL'],
                'rank': int(rank),
                'total_users': int(total_users),
                'badges': badges,
                'breakdown_plot': generate_breakdown_plot(user_df),
                'comparison_plot': generate_comparison_plot(user_df, all_users_df),
                'user_data': user_row_dict
            }

            if health_data.get('auto_email'):
                threading.Thread(
                    target=send_health_email_async,
                    args=(
                        health_data['email'],
                        health_data['first_name'],
                        float(user_row['HealthScore']),
                        user_row['ScoreDescription'],
                        rank,
                        total_users,
                        user_row['CourseTitle'],
                        user_row['CourseURL'],
                        language
                    )
                ).start()
                flash(trans['Check Inbox'], 'success')

            flash(trans['Submission Success'], 'success')
            return redirect(url_for('health_dashboard', step=1))

        except Exception as e:
            logger.error(f"Error in health_score_step3: {e}")
            flash(trans['Error processing data. Please try again.'], 'error')

    if form.errors:
        for field, errors in form.errors.items():
            for error in errors:
                logger.error(f"Validation error in {field}: {error}")
        flash(trans['Please correct the errors below'], 'error')

    return render_template(
        'health_score.html',
        form=form,
        step=3,
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        language=language
    )

@app.route('/health_dashboard', methods=['GET'])
def health_dashboard():
    step = request.args.get('step', default=1, type=int)
    if step not in range(1, 7):
        flash(get_translations(session.get('language', 'en'))['Invalid Step'], 'error')
        return redirect(url_for('health_dashboard', step=1))

    language = session.get('language', 'en')
    trans = get_translations(language)
    dashboard_data = session.get('dashboard_data', {})

    if not dashboard_data or 'email' not in dashboard_data:
        flash(trans['Session Expired'], 'error')
        return redirect(url_for('health_score_step1'))

    email = dashboard_data['email']
    try:
        user_df = fetch_data_from_sheet(email=email, headers=PREDETERMINED_HEADERS_HEALTH, worksheet_name='Health')
        all_users_df = fetch_data_from_sheet(headers=PREDETERMINED_HEADERS_HEALTH, worksheet_name='Health')

        if user_df.empty:
            flash(trans['Error retrieving data. Please try again.'], 'error')
            return redirect(url_for('health_score_step1'))

        user_df = calculate_health_score(user_df)
        all_users_df = calculate_health_score(all_users_df)
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_row = user_df.iloc[0]

        badges = assign_badges_health(user_df, all_users_df)
        all_scores = all_users_df['HealthScore'].astype(float).sort_values(ascending=False)
        rank = (all_scores >= user_row['HealthScore']).sum()
        total_users = len(all_scores)

        breakdown_plot = generate_breakdown_plot(user_df)
        comparison_plot = generate_comparison_plot(user_df, all_users_df)

        template_data = {
            'trans': trans,
            'user_data': dashboard_data['user_data'],
            'badges': badges,
            'rank': rank,
            'total_users': total_users,
            'health_score': dashboard_data['health_score'],
            'first_name': sanitize_input(dashboard_data.get('first_name', 'User')),
            'email': sanitize_input(email),
            'breakdown_plot': breakdown_plot,
            'comparison_plot': comparison_plot,
            'course_title': dashboard_data['course_title'],
            'course_url': dashboard_data['course_url'],
            'step': step,
            'FEEDBACK_FORM_URL': FEEDBACK_FORM_URL,
            'WAITLIST_FORM_URL': WAITLIST_FORM_URL,
            'CONSULTANCY_FORM_URL': CONSULTANCY_FORM_URL,
            'LINKEDIN_URL': LINKEDIN_URL,
            'TWITTER_URL': TWITTER_URL,
            'FACEBOOK_URL': FACEBOOK_URL,
            'language': language
        }

        if step == 6:
            session.pop('health_data', None)
            session.pop('dashboard_data', None)
            session.modified = True

        return render_template('health_dashboard.html', **template_data)

    except Exception as e:
        logger.error(f"Error rendering health dashboard: {e}")
        flash(trans['Error retrieving data. Please try again.'], 'error')
        return redirect(url_for('health_score_step1'))

@app.route('/quiz_step1', methods=['GET', 'POST'])
def quiz_step1():
    if not QUIZ_QUESTIONS:
        flash(get_translations(session.get('language', 'en'))['Quiz configuration error. Please try again later.'], 'error')
        return redirect(url_for('index'))

    language = session.get('language', 'en')
    trans = get_translations(language)
    
    preprocessed_questions = [
        {
            'id': f'question_{i+1}',
            'text': trans.get(q['text'], q['text']),
            'type': q['type'],
            'options': [trans.get(opt, opt) for opt in q['options']],
            'required': q.get('required', True)
        }
        for i, q in enumerate(QUIZ_QUESTIONS[:4])
    ]

    form = QuizForm(questions=preprocessed_questions, language=language)
    logger.debug(f"QuizStep1 form fields: {list(form._fields.keys())}")
    logger.debug(f"Preprocessed questions: {preprocessed_questions}")

    if request.method == 'POST':
        logger.debug(f"POST data: {request.form}")
        if form.validate_on_submit():
            session['quiz_data'] = session.get('quiz_data', {})
            try:
                session['quiz_data'].update({
                    q['id']: form[q['id']].data for q in preprocessed_questions if q['id'] in form._fields
                })
                session['language'] = form.language.data
                session.modified = True
                logger.info(f"Quiz step 1 validated successfully, updated session: {session['quiz_data']}")
                return redirect(url_for('quiz_step2'))
            except KeyError as e:
                logger.error(f"KeyError in quiz_step1 form processing: {e}")
                flash(trans['Form processing error. Please try again.'], 'error')
        else:
            logger.error(f"Form validation failed: {form.errors}")
            flash(trans['Please correct the errors below'], 'error')

    if 'quiz_data' in session:
        for q in preprocessed_questions:
            if q['id'] in session['quiz_data']:
                try:
                    getattr(form, q['id']).data = session['quiz_data'][q['id']]
                except AttributeError:
                    logger.warning(f"Field {q['id']} not found in form")

    progress = (4 / len(QUIZ_QUESTIONS)) * 100
    logger.debug(f"Form state before rendering: {form._fields}")
    return render_template(
        'quiz_step1.html',
        form=form,
        questions=preprocessed_questions,
        total_questions=len(QUIZ_QUESTIONS),
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        language=language,
        progress=progress
    )
@app.route('/quiz_step2', methods=['GET', 'POST'])
def quiz_step2():
    if 'quiz_data' not in session or not QUIZ_QUESTIONS:
        flash(get_translations(session.get('language', 'en'))['Session Expired'], 'error')
        return redirect(url_for('quiz_step1'))

    language = session.get('language', 'en')
    trans = get_translations(language)
    
    preprocessed_questions = [
        {
            'id': f'question_{i+1}',
            'text': trans.get(q['text'], q['text']),
            'type': q['type'],
            'options': [trans.get(opt, opt) for opt in q['options']],
            'required': q.get('required', True)
        }
        for i, q in enumerate(QUIZ_QUESTIONS[4:7])
    ]

    form = QuizForm(questions=preprocessed_questions, language=language)
    form.submit.label.text = trans['Next']
    form.back.label.text = trans['Previous']
    logger.debug(f"QuizStep2 form fields: {list(form._fields.keys())}")

    if request.method == 'POST':
        logger.debug(f"POST data: {request.form}")
        if form.validate_on_submit():
            if form.back.data:
                return redirect(url_for('quiz_step1'))
            try:
                session['quiz_data'].update({
                    q['id']: form[q['id']].data for q in preprocessed_questions if q['id'] in form._fields
                })
                session['language'] = form.language.data
                session.modified = True
                logger.info(f"Quiz step 2 validated successfully")
                return redirect(url_for('quiz_step3'))
            except KeyError as e:
                logger.error(f"KeyError in quiz_step2 form processing: {e}")
                flash(trans['Form processing error. Please try again.'], 'error')
        else:
            logger.error(f"Form validation failed: {form.errors}")
            flash(trans['Please correct the errors below'], 'error')

    if 'quiz_data' in session:
        for q in preprocessed_questions:
            if q['id'] in session['quiz_data']:
                try:
                    getattr(form, q['id']).data = session['quiz_data'][q['id']]
                except AttributeError:
                    logger.warning(f"Field {q['id']} not found in form")

    progress = (7 / len(QUIZ_QUESTIONS)) * 100
    return render_template(
        'quiz_step2.html',
        form=form,
        questions=preprocessed_questions,
        total_questions=len(QUIZ_QUESTIONS),
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        language=language,
        progress=progress
    )

@app.route('/quiz_step3', methods=['GET', 'POST'])
def quiz_step3():
    if 'quiz_data' not in session or not QUIZ_QUESTIONS:
        flash(get_translations(session.get('language', 'en'))['Session Expired'], 'error')
        return redirect(url_for('quiz_step1'))

    language = session.get('language', 'en')
    trans = get_translations(language)
    
    preprocessed_questions = [
        {
            'id': f'question_{i+1}',
            'text': trans.get(q['text'], q['text']),
            'type': q['type'],
            'options': [trans.get(opt, opt) for opt in q['options']],
            'required': q.get('required', True)
        }
        for i, q in enumerate(QUIZ_QUESTIONS[7:10])
    ]

    form = QuizForm(questions=preprocessed_questions, language=language)
    form.submit.label.text = trans['Submit Quiz']
    form.back.label.text = trans['Previous']
    logger.debug(f"QuizStep3 form fields: {list(form._fields.keys())}")

    if request.method == 'POST':
        logger.debug(f"POST data: {request.form}")
        if form.validate_on_submit():
            try:
                if form.back.data:
                    return redirect(url_for('quiz_step2'))
                
                quiz_data = session['quiz_data']
                quiz_data.update({
                    q['id']: form[q['id']].data for q in preprocessed_questions if q['id'] in form._fields
                })
                quiz_data.update({
                    'first_name': sanitize_input(form.first_name.data) if form.first_name.data else '',
                    'email': sanitize_input(form.email.data) if form.email.data else '',
                    'language': form.language.data,
                    'auto_email': form.auto_email.data
                })
                session['quiz_data'] = quiz_data
                session['language'] = form.language.data
                session.modified = True
                logger.info(f"Quiz step 3 validated successfully")

                # Use original QUIZ_QUESTIONS for answers to ensure correct indexing
                answers = [(QUIZ_QUESTIONS[int(k.split('_')[1]) - 1], v) for k, v in quiz_data.items() if k.startswith('question_')]
                personality, personality_desc, tip = assign_personality(answers, language)
                user_df = pd.DataFrame([{
                    'Timestamp': datetime.utcnow(),
                    'first_name': quiz_data.get('first_name', ''),
                    'email': quiz_data.get('email', ''),
                    'language': quiz_data.get('language', 'en'),
                    'personality': personality,
                    **{f'question_{i}': trans.get(QUIZ_QUESTIONS[i-1]['text'], QUIZ_QUESTIONS[i-1]['text']) for i in range(1, 11)},
                    **{f'answer_{i}': quiz_data.get(f'question_{i}', '') for i in range(1, 11)}
                }])
                all_users_df = fetch_data_from_sheet(headers=PREDETERMINED_HEADERS_QUIZ, worksheet_name='Quiz')
                badges = assign_badges_quiz(user_df, all_users_df)

                data = [
                    datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
                    quiz_data.get('first_name', ''),
                    quiz_data.get('email', ''),
                    quiz_data.get('language', 'en'),
                    *[trans.get(QUIZ_QUESTIONS[i-1]['text'], QUIZ_QUESTIONS[i-1]['text']) for i in range(1, 11)],
                    *[quiz_data.get(f'question_{i}', '') for i in range(1, 11)],
                    personality,
                    ','.join(badges),
                    str(form.auto_email.data).lower()
                ]

                if not append_to_sheet(data, PREDETERMINED_HEADERS_QUIZ, 'Quiz'):
                    flash(trans['Google Sheets Error'], 'error')
                    return redirect(url_for('quiz_step3'))

                results = {
                    'first_name': quiz_data.get('first_name', ''),
                    'personality': personality,
                    'personality_desc': personality_desc,
                    'tip': tip,
                    'badges': badges,
                    'answers': {trans.get(q['text'], q['text']): a for q, a in answers},
                    'summary_chart': generate_quiz_summary_chart(answers, language)
                }
                session['quiz_results'] = results
                session.modified = True

                if quiz_data.get('auto_email') and quiz_data.get('email'):
                    threading.Thread(
                        target=send_quiz_email_async,
                        args=(quiz_data['email'], quiz_data['first_name'], personality, personality_desc, tip, language)
                    ).start()
                    flash(trans['Check Inbox'], 'success')

                flash(trans['Submission Success'], 'success')
                return redirect(url_for('quiz_results'))

            except Exception as e:
                logger.error(f"Error processing quiz step 3: {e}")
                flash(trans['Error processing data. Please try again.'], 'error')

        else:
            logger.error(f"Form validation failed: {form.errors}")
            flash(trans['Please correct the errors below'], 'error')

    if 'quiz_data' in session:
        for q in preprocessed_questions:
            if q['id'] in session['quiz_data']:
                try:
                    getattr(form, q['id']).data = session['quiz_data'][q['id']]
                except AttributeError:
                    logger.warning(f"Field {q['id']} not found in form")

    progress = (10 / len(QUIZ_QUESTIONS)) * 100
    return render_template(
        'quiz_step3.html',
        form=form,
        questions=preprocessed_questions,
        total_questions=len(QUIZ_QUESTIONS),
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        language=language,
        progress=progress
    )
    
@app.route('/quiz_results', methods=['GET'])
def quiz_results():
    language = session.get('language', 'en')
    trans = get_translations(language)
    results = session.get('quiz_results', {})

    if not results:
        flash(trans['Session Expired'], 'error')
        return redirect(url_for('quiz_step1'))

    session.pop('quiz_data', None)
    session.pop('quiz_results', None)
    session.modified = True

    return render_template(
        'quiz_results.html',
        results=results,
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        language=language,
        debug_mode=app.config['DEBUG']
    )

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    language = session.get('language', 'en')
    trans = get_translations(language)
    email = session.get('budget_data', {}).get('email') or session.get('health_data', {}).get('email') or session.get('quiz_results', {}).get('email')
    session.clear()
    session.modified = True
    if email:
        backup_file = os.path.join(SESSION_BACKUP_DIR, f"{sanitize_filename(email)}.json")
        if os.path.exists(backup_file):
            try:
                os.remove(backup_file)
                logger.info(f"Deleted session backup for {email}")
            except Exception as e:
                logger.error(f"Failed to delete session backup for {email}: {e}")
    flash(trans['Logged Out Successfully'], 'success')
    return redirect(url_for('index'))

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.static_folder, 'favicon.ico', mimetype='image/x-icon')

@app.errorhandler(404)
def page_not_found(e):
    language = session.get('language', 'en')
    trans = get_translations(language)
    logger.error(f"404 error: {request.url}")
    return render_template(
        '404.html',
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TURN_OFF_JAVASCRIPT_URL=TURN_OFF_JAVASCRIPT_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        language=language
    ), 404

@app.errorhandler(500)
def internal_server_error(e):
    language = session.get('language', 'en')
    trans = get_translations(language)
    logger.error(f"500 error: {str(e)}")
    return render_template(
        '500.html',
        trans=trans,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        language=language
    ), 500

# Run the application
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

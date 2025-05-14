from flask import Flask, render_template, request, flash, redirect, url_for, session, send_from_directory
from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, SelectField, BooleanField, SubmitField, RadioField
from wtforms.validators import DataRequired, Email, Optional, ValidationError
from flask_session import Session
from flask_caching import Cache
from flask_mail import Mail, Message
from flask.sessions import SessionInterface, SecureCookieSession
import os
import logging
import json
import threading
from jinja2 import Environment
import time
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

# Configure server-side session
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(app.root_path, 'flask_session')
app.config['SESSION_PERMANENT'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = 3600
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_COOKIE_NAME'] = 'session_id'
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True in production
os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)

# Session backup directory
SESSION_BACKUP_DIR = os.path.join(app.root_path, 'session_backup')
os.makedirs(SESSION_BACKUP_DIR, exist_ok=True)

# Custom session interface for compression
class CompressedSession(SessionInterface):
    def open_session(self, app, request):
        session_data = request.cookies.get(self.get_cookie_name(app))
        if not session_data:
            logger.info("No session cookie found, creating new session")
            return SecureCookieSession()
        try:
            compressed_data = bytes.fromhex(session_data)
            decompressed_data = zlib.decompress(compressed_data).decode('utf-8')
            session = SecureCookieSession(json.loads(decompressed_data))
            if ('budget_data' not in session and 'health_data' not in session and 'quiz_results' not in session) and session.get('email'):
                session = self.restore_from_backup(session.get('email'), session)
            return session
        except Exception as e:
            logger.error(f"Error decompressing session data: {e}")
            return SecureCookieSession()

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
            session_data = json.dumps(dict(session)).encode('utf-8')
            compressed_data = zlib.compress(session_data)
            encoded_data = compressed_data.hex()
            response.set_cookie(
                self.get_cookie_name(app),
                encoded_data,
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
                session.modified = True
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

class HealthScoreForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    last_name = StringField('Last Name', validators=[Optional()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    phone_number = StringField('Phone Number', validators=[Optional()])
    business_name = StringField('Business Name', validators=[DataRequired()])
    user_type = SelectField('User Type', choices=[('SME', 'SME'), ('Individual', 'Individual')], validators=[DataRequired()])
    income_revenue = FloatField('Monthly Income/Revenue', validators=[DataRequired(), non_negative])
    expenses_costs = FloatField('Monthly Expenses/Costs', validators=[DataRequired(), non_negative])
    debt_loan = FloatField('Total Debt/Loan Amount', validators=[DataRequired(), non_negative])
    debt_interest_rate = FloatField('Debt Interest Rate (%)', validators=[DataRequired(), non_negative])
    language = SelectField('Language', choices=[('en', 'English'), ('ha', 'Hausa')], default='en')
    auto_email = BooleanField('Receive Email Report')
    submit = SubmitField()
    def __init__(self, language='en', *args, **kwargs):
        super(HealthScoreForm, self).__init__(*args, **kwargs)
        self.submit.label.text = get_translations(language)['Submit']

try:
    with open('questions.json', 'r', encoding='utf-8') as f:
        QUIZ_QUESTIONS = json.load(f)
    logger.debug(f"Successfully loaded QUIZ_QUESTIONS: {QUIZ_QUESTIONS}")
except FileNotFoundError:
    logger.error("questions.json file not found. Ensure it exists in the project root directory.")
    QUIZ_QUESTIONS = []  # Fallback to empty list to prevent crashes
except json.JSONDecodeError as e:
    logger.error(f"Error decoding questions.json: {e}")
    QUIZ_QUESTIONS = []  # Fallback to empty list to prevent crashes

class QuizStep1Form(FlaskForm):
    submit = SubmitField('Next')
    back = SubmitField('Back')
    def __init__(self, start_idx, end_idx, *args, **kwargs):
        super(QuizStep1Form, self).__init__(*args, **kwargs)
        for i in range(start_idx, end_idx):
            question = QUIZ_QUESTIONS[i]
            choices = [(opt, opt) for opt in question.get('options', ['Yes', 'No'])]
            field = RadioField(
                f'Question {i+1}',
                choices=choices,
                validators=[DataRequired()] if question.get('required', True) else [Optional()]
            )
            setattr(self, f'question_{i+1}', field)
            self._fields[f'question_{i+1}'] = field

class QuizStep2Form(FlaskForm):
    submit = SubmitField('Next')
    back = SubmitField('Back')
    def __init__(self, start_idx, end_idx, *args, **kwargs):
        super(QuizStep2Form, self).__init__(*args, **kwargs)
        for i in range(start_idx, end_idx):
            question = QUIZ_QUESTIONS[i]
            choices = [(opt, opt) for opt in question.get('options', ['Yes', 'No'])]
            field = RadioField(
                f'Question {i+1}',
                choices=choices,
                validators=[DataRequired()] if question.get('required', True) else [Optional()]
            )
            setattr(self, f'question_{i+1}', field)
            self._fields[f'question_{i+1}'] = field

class QuizStep3Form(FlaskForm):
    first_name = StringField('First Name', validators=[Optional()])
    email = StringField('Email', validators=[Optional(), Email()])
    language = SelectField('Language', choices=[('en', 'English'), ('ha', 'Hausa')], default='en')
    submit = SubmitField('Submit Quiz')
    back = SubmitField('Back')
    def __init__(self, start_idx, end_idx, *args, **kwargs):
        super(QuizStep3Form, self).__init__(*args, **kwargs)
        for i in range(start_idx, end_idx):
            question = QUIZ_QUESTIONS[i]
            choices = [(opt, opt) for opt in question.get('options', ['Yes', 'No'])]
            field = RadioField(
                f'Question {i+1}',
                choices=choices,
                validators=[DataRequired()] if question.get('required', True) else [Optional()]
            )
            setattr(self, f'question_{i+1}', field)
            self._fields[f'question_{i+1}'] = field

def assign_personality(answers, language='en'):
    trans = get_translations(language)
    score = sum(
        q.get('weight', 1) * (1 if a in q.get('positive_answers', ['Yes', 'Always']) else -1 if a in q.get('negative_answers', ['No', 'Never']) else 0)
        for q, a in answers
        if any(a in opt for opt in q.get('options', ['Yes', 'No']))
    )
    if score >= 6:
        return 'Planner', trans.get('Planner', 'You plan your finances well.'), trans.get('Planner Tip', 'Save regularly.')
    elif score >= 2:
        return 'Saver', trans.get('Saver', 'You save consistently.'), trans.get('Saver Tip', 'Increase your savings rate.')
    elif score == 0:
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
        language = user_row['language']
        trans = get_translations(language)
        if len(user_df) == 1:
            badges.append(trans['First Quiz Completed!'])
        if user_row['personality'] == 'Planner':
            badges.append(trans['Master Planner!'])
        elif user_row['personality'] == 'Avoider' and len(all_users_df) > 10:
            badges.append(trans['Needs Guidance!'])
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
        fig = px.bar(x=labels, y=values, title=get_translations(language).get('Quiz Summary', 'Quiz Summary'), labels={'x': 'Answer', 'y': 'Count'})
        fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), height=300, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
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
        # Clean and convert to float
        monthly_income = float(request.form.get('monthly_income', '0').replace(',', ''))
        session['budget_data']['monthly_income'] = monthly_income
        session.modified = True
        return redirect(url_for('budget_step3'))
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
        # Clean and convert to float
        session['budget_data'].update({
            'housing_expenses': float(request.form.get('housing_expenses', '0').replace(',', '')),
            'food_expenses': float(request.form.get('food_expenses', '0').replace(',', '')),
            'transport_expenses': float(request.form.get('transport_expenses', '0').replace(',', '')),
            'other_expenses': float(request.form.get('other_expenses', '0').replace(',', ''))
        })
        session.modified = True
        return redirect(url_for('budget_step4'))
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
        # Clean and convert to float
        savings_goal = float(request.form.get('savings_goal', '0').replace(',', '')) if request.form.get('savings_goal') else 0.0
        session['budget_data'].update({
            'savings_goal': savings_goal,
            'auto_email': form.auto_email.data
        })
        session.modified = True
        budget_data = session['budget_data']
        try:
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
                budget_data.get('auto_email', False),
                user_df['total_expenses'].iloc[0],
                user_df['savings'].iloc[0],
                user_df['surplus_deficit'].iloc[0],
                user_df['badges'].iloc[0],
                0,  # rank (calculated in dashboard)
                0   # total_users (calculated in dashboard)
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
    form = HealthScoreForm()
    trans = get_translations(form.language.data or 'en')

    if request.method == 'POST':
        # Validate Step 1 fields
        form.first_name.data = request.form.get('first_name')
        form.last_name.data = request.form.get('last_name')
        form.email.data = request.form.get('email')
        form.auto_email.data = 'auto_email' in request.form
        form.phone_number.data = request.form.get('phone_number')
        form.language.data = request.form.get('language')

        # Validate required fields
        if not form.first_name.data:
            flash(trans.get('First Name Required', 'First Name Required'), 'error')
            return render_template('health_score.html', form=form, step=1, trans=trans)
        if not form.validate():
            flash(trans.get('Please correct the errors below'), 'error')
            return render_template('health_score.html', form=form, step=1, trans=trans)
            flash(trans.get('Invalid Email', 'Invalid Email'), 'error')
            return render_template('health_score.html', form=form, step=1, trans=trans)
        if not form.language.data:
            flash(trans.get('Language required', 'Language required'), 'error')
            return render_template('health_score.html', form=form, step=1, trans=trans)

        # Store data in session
        session['health_score_step1'] = {
            'first_name': form.first_name.data,
            'last_name': form.last_name.data,
            'email': form.email.data,
            'auto_email': form.auto_email.data,
            'phone_number': form.phone_number.data,
            'language': form.language.data
        }
        return redirect(url_for('health_score_step2'))

    return render_template('health_score.html', form=form, step=1, trans=trans)

@app.route('/health_score_step2', methods=['GET', 'POST'])
def health_score_step2():
    # Ensure Step 1 data exists
    if 'health_score_step1' not in session:
        flash('Please complete Step 1 first.', 'error')
        return redirect(url_for('health_score_step1'))

    form = HealthScoreForm()
    trans = get_translations(session['health_score_step1']['language'])

    if request.method == 'POST':
        # Validate Step 2 fields
        form.business_name.data = request.form.get('business_name')
        form.user_type.data = request.form.get('user_type')

        if not form.business_name.data:
            flash(trans.get('Business Name Required', 'Business Name Required'), 'error')
            return render_template('health_score.html', form=form, step=2, trans=trans)
        if not form.user_type.data:
            flash('User Type is required.', 'error')
            return render_template('health_score.html', form=form, step=2, trans=trans)

        # Store data in session
        session['health_score_step2'] = {
            'business_name': form.business_name.data,
            'user_type': form.user_type.data
        }
        return redirect(url_for('health_score_step3'))

    return render_template('health_score.html', form=form, step=2, trans=trans)

@app.route('/health_score_step3', methods=['GET', 'POST'])
def health_score_step3():
    # Ensure previous steps are completed
    if 'health_score_step1' not in session or 'health_score_step2' not in session:
        flash('Please complete previous steps first.', 'error')
        return redirect(url_for('health_score_step1'))

    form = HealthScoreForm()
    trans = get_translations(session['health_score_step1']['language'])

    if request.method == 'POST':
        try:
            form.income_revenue.data = float(request.form.get('income_revenue', 0))
            form.expenses_costs.data = float(request.form.get('expenses_costs', 0))
            form.debt_loan.data = float(request.form.get('debt_loan', 0)) if request.form.get('debt_loan') else 0
            form.debt_interest_rate.data = float(request.form.get('debt_interest_rate', 0)) if request.form.get('debt_interest_rate') else 0

            if form.income_revenue.data < 0 or form.expenses_costs.data < 0:
                flash(trans.get('Value must be positive.', 'Value must be positive.'), 'error')
                return render_template('health_score.html', form=form, step=3, trans=trans)
            if form.debt_loan.data < 0 or form.debt_interest_rate.data < 0:
                flash(trans.get('Value must be positive.', 'Value must be positive.'), 'error')
                return render_template('health_score.html', form=form, step=3, trans=trans)

            # Combine all data
            health_score_data = {
                **session['health_score_step1'],
                **session['health_score_step2'],
                'income_revenue': form.income_revenue.data,
                'expenses_costs': form.expenses_costs.data,
                'debt_loan': form.debt_loan.data,
                'debt_interest_rate': form.debt_interest_rate.data
            }

            # Simple health score calculation (example logic)
            cash_flow = health_score_data['income_revenue'] - health_score_data['expenses_costs']
            debt_to_income = (health_score_data['debt_loan'] / health_score_data['income_revenue'] * 100) if health_score_data['income_revenue'] > 0 else 0
            debt_interest_burden = health_score_data['debt_interest_rate'] * (health_score_data['debt_loan'] / 100) if health_score_data['debt_loan'] > 0 else 0
            health_score = min(100, max(0, 50 + (cash_flow * 0.2) - (debt_to_income * 0.3) - (debt_interest_burden * 0.1)))

            # Store data for dashboard
            session['dashboard_data'] = {
                'first_name': health_score_data['first_name'],
                'email': health_score_data['email'],
                'health_score': health_score,
                'user_data': health_score_data,
                'rank': 1,  # Placeholder, replace with actual ranking logic
                'total_users': 100,  # Placeholder, replace with actual total users
                'breakdown_plot': True,  # Placeholder, replace with actual plot data
                'comparison_plot': True,  # Placeholder, replace with actual plot data
                'all_users_df': {'HealthScore': [health_score, 60, 70, 80, 90]},  # Placeholder data
                'badges': ['Positive Cash Flow'] if cash_flow > 0 else [],
                'course_url': 'https://example.com/course',
                'course_title': 'Financial Health 101'
            }

            # Clear previous step data
            session.pop('health_score_step1', None)
            session.pop('health_score_step2', None)

            # Redirect to dashboard with step 1
            return redirect(url_for('health_dashboard', step=1))

        except ValueError:
            flash(trans.get('Invalid Number', 'Invalid Number'), 'error')
            return render_template('health_score.html', form=form, step=3, trans=trans)

    return render_template('health_score.html', form=form, step=3, trans=trans)
        
@app.route('/health_dashboard/<int:step>', methods=['GET'])
def health_dashboard(step=1):
    # Validate step range
    if step < 1 or step > 6:
        flash(get_translations('en')['Error retrieving data. Please try again.'], 'error')
        return redirect(url_for('health_score'))

    # Check session data
    dashboard_data = session.get('dashboard_data', {})
    if not dashboard_data or 'email' not in dashboard_data:
        flash(get_translations('en')['Session Expired'], 'error')
        return redirect(url_for('health_score'))

    # Extract language and translation
    language = dashboard_data.get('language', 'en')
    trans = get_translations(language)

    # Extract user data from session
    email = dashboard_data['email']
    first_name = dashboard_data.get('first_name', 'User')

    try:
        # Fetch data from sheet
        user_df = fetch_data_from_sheet(email=email, headers=PREDETERMINED_HEADERS_HEALTH, worksheet_name='Health')
        if user_df.empty:
            flash(trans['Error retrieving data. Please try again.'], 'error')
            return redirect(url_for('health_score'))

        all_users_df = fetch_data_from_sheet(headers=PREDETERMINED_HEADERS_HEALTH, worksheet_name='Health')

        # Calculate health scores
        user_df = calculate_health_score(user_df)
        all_users_df = calculate_health_score(all_users_df)

        # Sort user data by timestamp
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_data = user_df.iloc[0].to_dict()

        # Get health score (use session value if available, otherwise from user_df)
        health_score = user_data.get('HealthScore', dashboard_data.get('health_score', 0.0))

        # Assign badges
        badges = assign_badges_health(user_df, all_users_df)

        # Calculate rank
        all_scores = all_users_df['HealthScore'].astype(float).sort_values(ascending=False)
        rank = (all_scores >= health_score).sum()
        total_users = len(all_scores)

        # Generate plots
        breakdown_plot = generate_breakdown_plot(user_df)
        comparison_plot = generate_comparison_plot(user_df, all_users_df)

        # Prepare template data
        template_data = {
            'trans': trans,
            'user_data': user_data,
            'badges': badges,
            'rank': rank,
            'total_users': total_users,
            'health_score': health_score,
            'first_name': sanitize_input(first_name),
            'email': sanitize_input(email),
            'step': step,
            'breakdown_plot': breakdown_plot,
            'comparison_plot': comparison_plot,
            'course_title': user_data.get('CourseTitle', trans['Financial Health Course']),
            'course_url': user_data.get('CourseURL', '#'),
            'all_users_df': all_users_df,
            'FEEDBACK_FORM_URL': FEEDBACK_FORM_URL,
            'WAITLIST_FORM_URL': WAITLIST_FORM_URL,
            'CONSULTANCY_FORM_URL': CONSULTANCY_FORM_URL,
            'LINKEDIN_URL': LINKEDIN_URL,
            'TWITTER_URL': TWITTER_URL,
            'FACEBOOK_URL': FACEBOOK_URL,
            'language': language
        }

        return render_template('health_dashboard.html', **template_data)

    except Exception as e:
        logger.error(f"Error rendering health dashboard: {e}")
        flash(trans['Error retrieving data. Please try again.'], 'error')
        return redirect(url_for('health_score'))
        
@app.route('/quiz_step1', methods=['GET', 'POST'])
def quiz_step1():
    language = session.get('language', 'en')
    trans = get_translations(language)
    form = QuizStep1Form(start_idx=0, end_idx=4)
    form.submit.label.text = trans['Next']
    if form.validate_on_submit():
        if form.back.data:
            return redirect(url_for('index'))
        quiz_data = session.get('quiz_data', {})
        for i in range(0, 4):
            quiz_data[f'question_{i+1}'] = getattr(form, f'question_{i+1}').data
        session['quiz_data'] = quiz_data
        session.modified = True
        return redirect(url_for('quiz_step2'))
    return render_template(
        'quiz_step1.html',
        form=form,
        questions=QUIZ_QUESTIONS[0:4],
        trans=trans,
        language=language,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        step=1
    )

@app.route('/quiz_step2', methods=['GET', 'POST'])
def quiz_step2():
    language = session.get('language', 'en')
    trans = get_translations(language)
    if 'quiz_data' not in session:
        flash(trans['Session Expired'], 'error')
        return redirect(url_for('quiz_step1'))
    form = QuizStep2Form(start_idx=4, end_idx=7)
    form.submit.label.text = trans['Next']
    form.back.label.text = trans['Back']
    if form.validate_on_submit():
        if form.back.data:
            return redirect(url_for('quiz_step1'))
        quiz_data = session['quiz_data']
        for i in range(4, 7):
            quiz_data[f'question_{i+1}'] = getattr(form, f'question_{i+1}').data
        session['quiz_data'] = quiz_data
        session.modified = True
        return redirect(url_for('quiz_step3'))
    return render_template(
        'quiz_step2.html',
        form=form,
        questions=QUIZ_QUESTIONS[4:7],
        trans=trans,
        language=language,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        step=2
    )

@app.route('/quiz_step3', methods=['GET', 'POST'])
def quiz_step3():
    language = session.get('language', 'en')
    trans = get_translations(language)
    if 'quiz_data' not in session:
        flash(trans['Session Expired'], 'error')
        return redirect(url_for('quiz_step1'))
    form = QuizStep3Form(start_idx=7, end_idx=10)
    form.submit.label.text = trans['Submit Quiz']
    form.back.label.text = trans['Back']
    if form.validate_on_submit():
        if form.back.data:
            return redirect(url_for('quiz_step2'))
        quiz_data = session['quiz_data']
        for i in range(7, 10):
            quiz_data[f'question_{i+1}'] = getattr(form, f'question_{i+1}').data
        quiz_data.update({
            'first_name': sanitize_input(form.first_name.data),
            'email': sanitize_input(form.email.data),
            'language': form.language.data,
            'auto_email': bool(form.email.data)
        })
        answers = [(QUIZ_QUESTIONS[i]['text'], quiz_data[f'question_{i+1}']) for i in range(10)]
        personality, personality_desc, tip = assign_personality([(QUIZ_QUESTIONS[i], quiz_data[f'question_{i+1}']) for i in range(10)], quiz_data['language'])
        data = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            quiz_data['first_name'],
            quiz_data['email'],
            quiz_data['language']
        ]
        for i, (question, answer) in enumerate(answers, 1):
            data.extend([question, answer])
        data.extend([personality, '', str(quiz_data['auto_email']).lower()])  # Badges to be assigned later
        if not append_to_sheet(data, PREDETERMINED_HEADERS_QUIZ, 'Quiz'):
            flash(trans.get('Google Sheets Error', 'Error saving to Google Sheets'), 'error')
            return redirect(url_for('quiz_step1'))
        user_df = fetch_data_from_sheet(email=quiz_data['email'], headers=PREDETERMINED_HEADERS_QUIZ, worksheet_name='Quiz')
        all_users_df = fetch_data_from_sheet(headers=PREDETERMINED_HEADERS_QUIZ, worksheet_name='Quiz')
        badges = assign_badges_quiz(user_df, all_users_df)
        summary_chart = generate_quiz_summary_chart(answers, quiz_data['language'])
        session['quiz_results'] = {
            'first_name': quiz_data['first_name'],
            'email': quiz_data['email'],
            'language': quiz_data['language'],
            'personality': personality,
            'personality_desc': personality_desc,
            'tip': tip,
            'answers': answers,
            'badges': badges,
            'summary_chart': summary_chart
        }
        session.modified = True
        if quiz_data['auto_email'] and quiz_data['email']:
            threading.Thread(
                target=send_quiz_email_async,
                args=(quiz_data['email'], quiz_data['first_name'] or 'User', personality, personality_desc, tip, quiz_data['language'])
            ).start()
            flash(trans.get('Check Inbox', 'Check your inbox for results'), 'success')
        flash(trans.get('Processing your results...'), 'info')
        time.sleep(1)  # Simulate processing delay
        flash(trans.get('Submission Success', 'Quiz submitted successfully'), 'success')
        return redirect(url_for('quiz_results'))
    return render_template(
        'quiz_step3.html',
        form=form,
        questions=QUIZ_QUESTIONS[7:10],
        trans=trans,
        language=language,
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        LINKEDIN_URL=LINKEDIN_URL,
        TWITTER_URL=TWITTER_URL,
        FACEBOOK_URL=FACEBOOK_URL,
        step=3
    )

@app.route('/quiz_results', methods=['GET', 'POST'])
def quiz_results():
    language = session.get('language', 'en')
    trans = get_translations(language)
    if 'quiz_results' not in session:
        flash(trans['Session Expired'], 'error')
        return redirect(url_for('quiz_step1'))
    quiz_results = session['quiz_results']
    if not quiz_results.get('email'):
        flash(trans['Error retrieving data. Please try again.'], 'error')
        return redirect(url_for('quiz_step1'))
    email = quiz_results['email']
    try:
        user_df = fetch_data_from_sheet(email=email, headers=PREDETERMINED_HEADERS_QUIZ, worksheet_name='Quiz')
        if user_df.empty:
            flash(trans['Error retrieving data. Please try again.'], 'error')
            return redirect(url_for('quiz_step1'))
        all_users_df = fetch_data_from_sheet(headers=PREDETERMINED_HEADERS_QUIZ, worksheet_name='Quiz')
        badges = assign_badges_quiz(user_df, all_users_df)
        quiz_results['badges'] = badges
        session['quiz_results'] = quiz_results
        session.modified = True
        return render_template(
            'quiz_results.html',
            results=quiz_results,
            trans=trans,
            FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
            WAITLIST_FORM_URL=WAITLIST_FORM_URL,
            CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
            LINKEDIN_URL=LINKEDIN_URL,
            TWITTER_URL=TWITTER_URL,
            FACEBOOK_URL=FACEBOOK_URL,
            language=language
        )
    except Exception as e:
        logger.error(f"Error rendering quiz results: {e}")
        flash(trans['Error retrieving data. Please try again.'], 'error')
        return redirect(url_for('quiz_step1'))

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    language = session.get('language', 'en')
    trans = get_translations(language)
    email = session.get('budget_data', {}).get('email') or session.get('health_data', {}).get('email') or session.get('quiz_results', {}).get('email')
    session.clear()
    session.modified = True
    if email:
        backup_file = os.path.join(SESSION_BACKUP_DIR, f"{sanitize_input(email)}.json")
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

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

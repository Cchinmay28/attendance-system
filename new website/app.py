from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import csv, ipaddress, os, shutil, socket, tempfile, threading
from datetime import datetime, timedelta
from dotenv import load_dotenv
import io
from functools import wraps
import logging

try:
    from supabase import create_client
except Exception:  # pragma: no cover - dependency may be absent until installed
    create_client = None

load_dotenv()


def is_vercel_environment():
    return bool(os.getenv('VERCEL')) or bool(os.getenv('VERCEL_ENV'))


app = Flask(__name__)
application = app
app.secret_key = os.getenv('SECRET_KEY', 'supersecretkey')
app.logger.setLevel(logging.INFO)

ADMIN_ID = os.getenv('ADMIN_ID', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
LOOPBACK_IPS = {'127.0.0.1', '::1'}


def parse_office_allowed_ips(raw_value):
    default_networks = ['192.168.0.0/24']
    if raw_value is None:
        return default_networks
    values = [x.strip() for x in raw_value.split(',') if x.strip()]
    values = [x for x in values if x not in LOOPBACK_IPS]
    if not values:
        return default_networks
    # A single office public/ISP IP (e.g. "103.91.222.26/32") is a valid,
    # intentional configuration and must be honored as-is - it is how a
    # publicly deployed site is locked to one office's ISP-assigned address.
    return values


OFFICE_ALLOWED_IPS = parse_office_allowed_ips(os.getenv('OFFICE_ALLOWED_IPS'))
TRUSTED_PROXY_IPS = [x.strip() for x in os.getenv('TRUSTED_PROXY_IPS', '').split(',') if x.strip()]
TRUSTED_PROXY_IPS = TRUSTED_PROXY_IPS or []
DEMO_MODE = os.getenv('DEMO_MODE', 'false').lower() == 'true'
PUBLIC_DEPLOYMENT = os.getenv('PUBLIC_DEPLOYMENT', 'false').lower() in ('1', 'true', 'yes')
ALLOW_LOOPBACK = os.getenv('ALLOW_LOCALHOST', 'false').lower() in ('1', 'true', 'yes')


def is_demo_mode():
    return bool(DEMO_MODE)


def is_public_deployment():
    return bool(PUBLIC_DEPLOYMENT)


def allow_loopback():
    return bool(ALLOW_LOOPBACK)

WORK_HOURS = 8  # Company standard working hours
SUPABASE_URL = os.getenv('SUPABASE_URL', '').strip()
SUPABASE_KEY = (os.getenv('SUPABASE_SERVICE_ROLE_KEY') or os.getenv('SUPABASE_ANON_KEY') or os.getenv('SUPABASE_KEY', '')).strip()
USE_SUPABASE = os.getenv('USE_SUPABASE', 'false').lower() in ('1', 'true', 'yes')
SUPABASE_ENABLED = bool(USE_SUPABASE and SUPABASE_URL and SUPABASE_KEY and create_client)
SUPABASE_CLIENT = None
if SUPABASE_ENABLED:
    try:
        SUPABASE_CLIENT = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as exc:
        app.logger.warning('Supabase client initialization failed: %s', exc)
        SUPABASE_ENABLED = False

EMPLOYEES_CSV = 'employees.csv'
ATTENDANCE_CSV = 'attendance_records.csv'
DENIED_CSV = 'denied_attempts.csv'
LOGIN_LOGS_CSV = 'login_logs.csv'
OFFICES_CSV = 'offices.csv'
BREAKS_CSV = 'breaks.csv'
# in project folder
 
def get_data_dir():
    candidates = [
        os.path.join(app.root_path, 'data'),
        os.path.join(tempfile.gettempdir(), 'office-attendance-app-data')
    ]
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            if os.access(path, os.W_OK):
                return path
        except OSError:
            continue
    return tempfile.gettempdir()

DATA_DIR = get_data_dir()


def get_storage_path(filename):
    return os.path.join(DATA_DIR, filename)

ATT_HEADERS = ['record_id','employee_id','employee_name','department','office','date',
               'clock_in_time','clock_out_time','total_hours','late_minutes','extra_hours',
               'clock_in_ip','clock_out_ip','status','late','total_break_time','total_working_time',
               'net_working_time','break_count','on_break','current_break_id','early_departure_minutes']
EMP_HEADERS = ['employee_id','name','department','office','password','username','shift_start','shift_end','allowed_ips','enabled']
DENIED_HEADERS = ['timestamp','employee_id','action','detected_ip','reason','employee_name','isp','browser','operating_system','device','status']
LOGIN_LOG_HEADERS = ['timestamp','employee_name','employee_id','user_id','role','activity_type','date','time','detected_ip','isp','browser','operating_system','device','status']
OFFICE_HEADERS = ['office_id','office_name','city']
BREAK_HEADERS = ['break_id','attendance_id','employee_id','date','break_start','break_end','duration_seconds','duration_display','break_start_ip','break_end_ip']

DEFAULT_SHIFT_START = '09:00'
DEFAULT_SHIFT_END = '18:00'
ACCESS_DENIED_MESSAGE = 'Access Denied: Your current network is not authorized for this account.'
CSV_LOCKS = {}
CSV_LOCKS_GUARD = threading.Lock()


def csv_lock(path):
    name = os.path.basename(path)
    with CSV_LOCKS_GUARD:
        if name not in CSV_LOCKS:
            CSV_LOCKS[name] = threading.RLock()
        return CSV_LOCKS[name]

def get_table_name(path):
    mapping = {
        'employees.csv': 'employees',
        'attendance_records.csv': 'attendance',
        'denied_attempts.csv': 'denied_attempts',
        'login_logs.csv': 'login_logs',
        'offices.csv': 'offices',
        'breaks.csv': 'breaks',
    }
    return mapping.get(os.path.basename(path), os.path.splitext(os.path.basename(path))[0])


def ensure_csv(path, headers):
    storage_path = get_storage_path(os.path.basename(path))
    if os.path.exists(storage_path):
        migrate_csv_headers(path, headers)
        return

    if SUPABASE_ENABLED:
        table_name = get_table_name(path)
        try:
            response = SUPABASE_CLIENT.table(table_name).select('*').execute()
            if response.data:
                return
        except Exception:
            pass

        if table_name == 'offices':
            seed_rows = [
                {'office_id': 'OFF001', 'office_name': 'Head Office', 'city': 'Mumbai'},
                {'office_id': 'OFF002', 'office_name': 'Branch Office', 'city': 'Delhi'},
                {'office_id': 'OFF003', 'office_name': 'South Office', 'city': 'Bangalore'},
            ]
        elif table_name == 'employees':
            seed_rows = [
                {'employee_id': '101', 'name': 'John Smith', 'department': 'Recruiting', 'office': 'Head Office', 'password': 'pass101', 'username': '101', 'shift_start': DEFAULT_SHIFT_START, 'shift_end': DEFAULT_SHIFT_END, 'allowed_ips': '', 'enabled': 'Yes'},
                {'employee_id': '102', 'name': 'Aisha Khan', 'department': 'Sales', 'office': 'Branch Office', 'password': 'pass102', 'username': '102', 'shift_start': DEFAULT_SHIFT_START, 'shift_end': DEFAULT_SHIFT_END, 'allowed_ips': '', 'enabled': 'Yes'},
                {'employee_id': '103', 'name': 'Ravi Patel', 'department': 'Operations', 'office': 'South Office', 'password': 'pass103', 'username': '103', 'shift_start': DEFAULT_SHIFT_START, 'shift_end': DEFAULT_SHIFT_END, 'allowed_ips': '', 'enabled': 'Yes'},
            ]
        else:
            seed_rows = []
        if seed_rows:
            try:
                SUPABASE_CLIENT.table(table_name).insert(seed_rows).execute()
            except Exception:
                pass

    with open(storage_path, 'w', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow(headers)


def backup_csv(storage_path):
    if not os.path.exists(storage_path):
        return
    backup_dir = os.path.join(os.path.dirname(storage_path), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    stamp = app_now().strftime('%Y%m%d%H%M%S')
    backup_path = os.path.join(backup_dir, f'{os.path.basename(storage_path)}.{stamp}.bak')
    shutil.copy2(storage_path, backup_path)


def coerce_row(headers, row):
    return {header: (row.get(header, '') if row else '') for header in headers}


def migrate_csv_headers(path, headers):
    storage_path = get_storage_path(os.path.basename(path))
    with csv_lock(path):
        try:
            with open(storage_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                current_headers = reader.fieldnames or []
                rows = [coerce_row(headers, row) for row in reader]
        except (OSError, csv.Error) as exc:
            app.logger.exception('CSV migration read failed for %s', storage_path)
            return
        if current_headers == headers:
            return
        app.logger.info('Migrating CSV headers for %s', storage_path)
        write_csv(path, headers, rows, make_backup=True)


def validate_ip_list(raw_value):
    cleaned = []
    for value in split_allowed_ips(raw_value):
        if value == '*' or value.endswith('.*'):
            cleaned.append(value)
            continue
        try:
            if '/' in value:
                cleaned.append(str(ipaddress.ip_network(value, strict=False)))
            else:
                cleaned.append(str(ipaddress.ip_address(value)))
        except ValueError as exc:
            raise ValueError(f'Invalid allowed IP address: {value}') from exc
    return ','.join(cleaned)


def validate_csv_rows(filename, headers, rows):
    clean = [coerce_row(headers, row) for row in rows]
    if os.path.basename(filename) == EMPLOYEES_CSV:
        seen_ids, seen_users, seen_records = set(), set(), set()
        for row in clean:
            row['employee_id'] = row.get('employee_id', '').strip()
            row['username'] = (row.get('username') or row['employee_id']).strip()
            row['enabled'] = normalize_enabled(row.get('enabled'))
            row['allowed_ips'] = validate_ip_list(row.get('allowed_ips', ''))
            record_key = tuple((row.get(h, '') or '').strip().lower() for h in EMP_HEADERS if h != 'password')
            if row['employee_id'].lower() in seen_ids:
                raise ValueError(f'Duplicate employee ID: {row["employee_id"]}')
            if row['username'].lower() in seen_users:
                raise ValueError(f'Duplicate username: {row["username"]}')
            if record_key in seen_records:
                raise ValueError(f'Duplicate employee record: {row["employee_id"]}')
            seen_ids.add(row['employee_id'].lower())
            seen_users.add(row['username'].lower())
            seen_records.add(record_key)
    elif os.path.basename(filename) == ATTENDANCE_CSV:
        seen_records, seen_active = set(), set()
        for row in clean:
            record_id = row.get('record_id', '').strip()
            if record_id and record_id in seen_records:
                raise ValueError(f'Duplicate attendance record ID: {record_id}')
            seen_records.add(record_id)
            active_key = (row.get('employee_id', ''), row.get('date', ''))
            if row.get('status') == 'Active':
                if active_key in seen_active:
                    raise ValueError(f'Duplicate active attendance for employee {active_key[0]} on {active_key[1]}')
                seen_active.add(active_key)
    elif os.path.basename(filename) == BREAKS_CSV:
        seen_breaks = set()
        for row in clean:
            break_id = row.get('break_id', '').strip()
            if break_id and break_id in seen_breaks:
                raise ValueError(f'Duplicate break record ID: {break_id}')
            seen_breaks.add(break_id)
    elif os.path.basename(filename) == LOGIN_LOGS_CSV:
        seen_logs = set()
        for row in clean:
            key = (row.get('timestamp', ''), row.get('employee_id') or row.get('user_id', ''), row.get('activity_type', ''), row.get('detected_ip', ''), row.get('status', ''))
            if key in seen_logs:
                raise ValueError('Duplicate activity log record.')
            seen_logs.add(key)
    elif os.path.basename(filename) == DENIED_CSV:
        seen_denied = set()
        for row in clean:
            key = (row.get('timestamp', ''), row.get('employee_id', ''), row.get('action', ''), row.get('detected_ip', ''), row.get('reason', ''))
            if key in seen_denied:
                raise ValueError('Duplicate access denied log record.')
            seen_denied.add(key)
    return clean


def read_csv(path):
    global SUPABASE_ENABLED
    storage_path = get_storage_path(os.path.basename(path))
    if os.path.exists(storage_path):
        with csv_lock(path):
            try:
                with open(storage_path, 'r', newline='', encoding='utf-8') as f:
                    return list(csv.DictReader(f))
            except (OSError, csv.Error, UnicodeDecodeError):
                app.logger.exception('CSV read failed for %s', storage_path)
                backup_csv(storage_path)
                return []

    if SUPABASE_ENABLED:
        table_name = get_table_name(path)
        try:
            response = SUPABASE_CLIENT.table(table_name).select('*').execute()
            if response.data:
                return response.data
        except Exception as exc:
            app.logger.warning('Supabase read failed for %s: %s', table_name, exc)
            SUPABASE_ENABLED = False

    return []


def write_csv(path, headers, rows, make_backup=True):
    global SUPABASE_ENABLED
    storage_path = get_storage_path(os.path.basename(path))
    rows = validate_csv_rows(path, headers, rows)
    if SUPABASE_ENABLED:
        table_name = get_table_name(path)
        try:
            key_field = {
                'employees': 'employee_id',
                'attendance': 'record_id',
                'denied_attempts': 'timestamp',
                'login_logs': 'timestamp',
                'offices': 'office_id',
                'breaks': 'break_id',
            }.get(table_name)
            if key_field:
                SUPABASE_CLIENT.table(table_name).delete().neq(key_field, '').execute()
            else:
                SUPABASE_CLIENT.table(table_name).delete().execute()
            if rows:
                SUPABASE_CLIENT.table(table_name).insert(rows).execute()
        except Exception as exc:
            app.logger.exception('Supabase write failed for %s', table_name)
            SUPABASE_ENABLED = False

    temp_path = None
    with csv_lock(path):
        try:
            os.makedirs(os.path.dirname(storage_path), exist_ok=True)
            if make_backup:
                backup_csv(storage_path)
            fd, temp_path = tempfile.mkstemp(prefix='attendance_', suffix='.tmp', dir=os.path.dirname(storage_path))
            with os.fdopen(fd, 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
                w.writeheader()
                w.writerows(rows)
            os.replace(temp_path, storage_path)
            app.logger.info('CSV saved: %s (%s rows)', storage_path, len(rows))
        except Exception:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
            raise


def app_now():
    """Single app clock used by live UI, attendance writes, and audit logs."""
    return datetime.now().astimezone()


def iso_timestamp(dt=None):
    return (dt or app_now()).isoformat(timespec='seconds')


def parse_hhmm(value, fallback):
    value = (value or '').strip()
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.strptime(value, fmt).strftime('%H:%M')
        except ValueError:
            continue
    return fallback


def normalize_enabled(value):
    value = str(value if value is not None else 'Yes').strip().lower()
    return 'No' if value in ('no', 'false', '0', 'disabled', 'inactive') else 'Yes'


def normalize_employee_record(record):
    record = dict(record)
    record.setdefault('employee_id', '')
    record.setdefault('name', '')
    record.setdefault('department', '')
    record.setdefault('office', 'Head Office')
    record.setdefault('password', '')
    record.setdefault('username', record.get('employee_id', ''))
    record.setdefault('shift_start', DEFAULT_SHIFT_START)
    record.setdefault('shift_end', DEFAULT_SHIFT_END)
    record.setdefault('allowed_ips', '')
    record.setdefault('enabled', 'Yes')
    if not record.get('office'):
        record['office'] = 'Head Office'
    if not record.get('username'):
        record['username'] = record.get('employee_id', '')
    record['shift_start'] = parse_hhmm(record.get('shift_start'), DEFAULT_SHIFT_START)
    record['shift_end'] = parse_hhmm(record.get('shift_end'), DEFAULT_SHIFT_END)
    record['enabled'] = normalize_enabled(record.get('enabled'))
    return {header: record.get(header, '') for header in EMP_HEADERS}


def read_employees():
    return [normalize_employee_record(e) for e in read_csv(EMPLOYEES_CSV)]


def write_employees(employees):
    write_csv(EMPLOYEES_CSV, EMP_HEADERS, [normalize_employee_record(e) for e in employees])


def get_employee(employee_id):
    return next((e for e in read_employees() if e.get('employee_id') == employee_id), None)


def split_allowed_ips(raw_value):
    return [x.strip() for x in (raw_value or '').split(',') if x.strip()]


def is_ip_allowed_by_list(ip, allowed_values):
    ip = normalize_ip(ip)
    if not allowed_values:
        return ip_ok(ip)
    for allowed in allowed_values:
        if allowed == '*':
            return True
        if allowed == ip:
            return True
        if allowed.endswith('.*') and ip.startswith(allowed[:-2] + '.'):
            return True
        if '/' in allowed:
            try:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(allowed, strict=False):
                    return True
            except ValueError:
                continue
    return False


def employee_ip_ok(employee_id, ip):
    if is_demo_mode():
        return True
    employee = get_employee(employee_id)
    if not employee:
        return ip_ok(ip)
    if employee.get('enabled') == 'No':
        return False
    return is_ip_allowed_by_list(ip, split_allowed_ips(employee.get('allowed_ips')))


def parse_user_agent(user_agent):
    ua = (user_agent or '').strip()
    lower = ua.lower()
    if 'edg/' in lower:
        browser = 'Microsoft Edge'
    elif 'chrome/' in lower and 'chromium' not in lower:
        browser = 'Chrome'
    elif 'firefox/' in lower:
        browser = 'Firefox'
    elif 'safari/' in lower and 'chrome/' not in lower:
        browser = 'Safari'
    else:
        browser = 'Unknown'

    if 'windows' in lower:
        os_name = 'Windows'
    elif 'android' in lower:
        os_name = 'Android'
    elif 'iphone' in lower or 'ipad' in lower:
        os_name = 'iOS'
    elif 'mac os' in lower or 'macintosh' in lower:
        os_name = 'macOS'
    elif 'linux' in lower:
        os_name = 'Linux'
    else:
        os_name = 'Unknown'

    if 'mobile' in lower or 'iphone' in lower or 'android' in lower:
        device = 'Mobile'
    elif 'ipad' in lower or 'tablet' in lower:
        device = 'Tablet'
    elif ua:
        device = 'Desktop'
    else:
        device = 'Unknown'
    return browser, os_name, device


def detect_isp(ip):
    ip = normalize_ip(ip)
    if not ip:
        return 'Unknown'
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback:
            return 'Private/Local Network'
    except ValueError:
        return 'Unknown'
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return 'Unknown'


def activity_user(employee_id, role):
    if role == 'admin':
        return 'Administrator', employee_id or ADMIN_ID
    employee = get_employee(employee_id)
    if employee:
        return employee.get('name') or 'Unknown', employee.get('employee_id') or employee_id
    return session.get('employee_name') or 'Unknown', employee_id or session.get('employee_id') or 'unknown'


def is_recent_duplicate_log(rows, candidate, seconds=2):
    for row in reversed(rows[-10:]):
        same_event = all((row.get(k, '') or '') == (candidate.get(k, '') or '') for k in ('employee_id', 'activity_type', 'detected_ip', 'status'))
        if not same_event:
            continue
        try:
            previous = datetime.fromisoformat(row.get('timestamp', ''))
            current = datetime.fromisoformat(candidate.get('timestamp', ''))
            if abs((current - previous).total_seconds()) <= seconds:
                return True
        except (TypeError, ValueError):
            if row.get('date') == candidate.get('date') and row.get('time') == candidate.get('time'):
                return True
    return False


def log_activity(employee_id, role, activity_type, detected_ip, status='success', when=None):
    when = when or app_now()
    employee_name, normalized_employee_id = activity_user(employee_id, role)
    browser, os_name, device = parse_user_agent(request.headers.get('User-Agent', '') if request else '')
    logins = read_csv(LOGIN_LOGS_CSV)
    row = {
        'timestamp': iso_timestamp(when),
        'employee_name': employee_name,
        'employee_id': normalized_employee_id,
        'user_id': normalized_employee_id,
        'role': role or 'unknown',
        'activity_type': activity_type or 'unknown',
        'date': when.strftime('%Y-%m-%d'),
        'time': when.strftime('%H:%M:%S'),
        'detected_ip': detected_ip or 'unknown',
        'isp': detect_isp(detected_ip),
        'browser': browser,
        'operating_system': os_name,
        'device': device,
        'status': status or 'success',
    }
    if is_recent_duplicate_log(logins, row):
        app.logger.info('Skipped duplicate activity log: %s %s %s', normalized_employee_id, activity_type, detected_ip)
        return
    logins.append(row)
    write_csv(LOGIN_LOGS_CSV, LOGIN_LOG_HEADERS, logins)


def log_denied_attempt(employee_id, action, detected_ip, reason):
    denied = read_csv(DENIED_CSV)
    now = app_now()
    employee_name, normalized_employee_id = activity_user(employee_id, 'employee' if employee_id != ADMIN_ID else 'admin')
    browser, os_name, device = parse_user_agent(request.headers.get('User-Agent', '') if request else '')
    row = {
        'timestamp': iso_timestamp(now),
        'employee_id': normalized_employee_id,
        'action': action or 'unknown',
        'detected_ip': detected_ip or 'unknown',
        'reason': reason or 'IP not in allowed list',
        'employee_name': employee_name,
        'isp': detect_isp(detected_ip),
        'browser': browser,
        'operating_system': os_name,
        'device': device,
        'status': 'denied',
    }
    duplicate = False
    for existing in reversed(denied[-10:]):
        duplicate = all(existing.get(k, '') == row.get(k, '') for k in ('employee_id', 'action', 'detected_ip', 'reason'))
        if duplicate:
            try:
                duplicate = abs((datetime.fromisoformat(row['timestamp']) - datetime.fromisoformat(existing.get('timestamp', ''))).total_seconds()) <= 2
            except (TypeError, ValueError):
                duplicate = existing.get('timestamp') == row.get('timestamp')
        if duplicate:
            app.logger.info('Skipped duplicate denied log: %s %s %s', normalized_employee_id, action, detected_ip)
            return
    denied.append(row)
    write_csv(DENIED_CSV, DENIED_HEADERS, denied)


def log_login(user_id, role, detected_ip, status='success'):
    log_activity(user_id, role, 'Login', detected_ip, status)

def format_duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 0:
        seconds = 0
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'


def format_minutes_display(minutes):
    minutes = int(float(minutes or 0))
    if minutes <= 0:
        return 'On time'
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f'{hours}h {mins}m late'
    if hours:
        return f'{hours}h late'
    return f'{mins}m late'


def format_hours_display(hours):
    total_minutes = int(round(float(hours or 0) * 60))
    if total_minutes <= 0:
        return '0m'
    hrs, mins = divmod(total_minutes, 60)
    if hrs and mins:
        return f'{hrs}h {mins}m'
    if hrs:
        return f'{hrs}h'
    return f'{mins}m'


def parse_duration_seconds(value):
    if value in (None, ''):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        if ':' in value:
            parts = [int(p) for p in value.split(':')]
            if len(parts) == 3:
                hours, minutes, seconds = parts
                return hours * 3600 + minutes * 60 + seconds
            if len(parts) == 2:
                minutes, seconds = parts
                return minutes * 60 + seconds
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def normalize_attendance_record(record):
    record = dict(record)
    record.setdefault('total_break_time', '00:00:00')
    record.setdefault('total_working_time', '00:00:00')
    record.setdefault('net_working_time', '00:00:00')
    record.setdefault('break_count', '0')
    record.setdefault('on_break', 'No')
    record.setdefault('current_break_id', '')
    record.setdefault('early_departure_minutes', '0')
    if not record.get('total_working_time'):
        if record.get('total_hours'):
            try:
                record['total_working_time'] = format_duration(int(float(record['total_hours']) * 3600))
            except (TypeError, ValueError):
                record['total_working_time'] = '00:00:00'
        else:
            record['total_working_time'] = '00:00:00'
    if not record.get('net_working_time'):
        record['net_working_time'] = record.get('total_working_time', '00:00:00')
    if not record.get('total_break_time'):
        record['total_break_time'] = '00:00:00'
    return record


def normalize_break_record(record):
    record = dict(record)
    record.setdefault('duration_seconds', '0')
    record.setdefault('duration_display', '00:00:00')
    record.setdefault('break_start_ip', '')
    record.setdefault('break_end_ip', '')
    return record


def read_attendance_records():
    return [normalize_attendance_record(r) for r in read_csv(ATTENDANCE_CSV)]


def write_attendance_records(records):
    write_csv(ATTENDANCE_CSV, ATT_HEADERS, [normalize_attendance_record(r) for r in records])


def read_break_records():
    attendance_ids = {r.get('record_id') for r in read_attendance_records()}
    breaks = [normalize_break_record(r) for r in read_csv(BREAKS_CSV)]
    return [b for b in breaks if not b.get('attendance_id') or b.get('attendance_id') in attendance_ids]


def write_break_records(records):
    write_csv(BREAKS_CSV, BREAK_HEADERS, [normalize_break_record(r) for r in records])


def sync_break_totals(attendance, breaks):
    completed_breaks = [b for b in breaks if b.get('attendance_id') == attendance.get('record_id') and b.get('break_end')]
    total_break_seconds = sum(parse_duration_seconds(b.get('duration_seconds')) for b in completed_breaks)
    active_break = next((b for b in breaks if b.get('attendance_id') == attendance.get('record_id') and not b.get('break_end')), None)
    attendance['total_break_time'] = format_duration(total_break_seconds)
    attendance['break_count'] = str(len(completed_breaks))
    attendance['on_break'] = 'Yes' if active_break else 'No'
    attendance['current_break_id'] = active_break.get('break_id', '') if active_break else ''


def next_numeric_id(rows, field):
    values = []
    for row in rows:
        try:
            values.append(int(row.get(field) or 0))
        except (TypeError, ValueError):
            continue
    return str((max(values) if values else 0) + 1)


def parse_time_for_date(date_str, time_str):
    if not date_str or not time_str:
        return None
    return datetime.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M:%S')


def break_duration_seconds(break_record, now=None):
    now = now or app_now().replace(tzinfo=None)
    if getattr(now, 'tzinfo', None):
        now = now.replace(tzinfo=None)
    if break_record.get('break_end'):
        return parse_duration_seconds(break_record.get('duration_seconds'))
    started = parse_time_for_date(break_record.get('date'), break_record.get('break_start'))
    if not started:
        return 0
    return max(0, int((now - started).total_seconds()))


def get_breaks_for_attendance(breaks, attendance_id):
    scoped = [normalize_break_record(b) for b in breaks if b.get('attendance_id') == attendance_id]
    return sorted(scoped, key=lambda b: (b.get('date', ''), b.get('break_start', ''), b.get('break_id', '')))


def serialize_break(break_record, now=None):
    duration_seconds = break_duration_seconds(break_record, now)
    data = normalize_break_record(break_record)
    data['duration_seconds'] = str(duration_seconds)
    data['duration_display'] = format_duration(duration_seconds)
    data['active'] = not bool(data.get('break_end'))
    return data


def serialize_attendance(record, breaks, now=None):
    now = now or app_now().replace(tzinfo=None)
    if getattr(now, 'tzinfo', None):
        now = now.replace(tzinfo=None)
    attendance = normalize_attendance_record(record)
    scoped_breaks = get_breaks_for_attendance(breaks, attendance.get('record_id'))
    sync_break_totals(attendance, scoped_breaks)
    completed_seconds = sum(break_duration_seconds(b, now) for b in scoped_breaks if b.get('break_end'))
    active_seconds = sum(break_duration_seconds(b, now) for b in scoped_breaks if not b.get('break_end'))

    gross_seconds = parse_duration_seconds(attendance.get('total_working_time'))
    clock_in = parse_time_for_date(attendance.get('date'), attendance.get('clock_in_time'))
    if attendance.get('status') == 'Active' and clock_in:
        gross_seconds = max(0, int((now - clock_in).total_seconds()))
    elif not gross_seconds and attendance.get('clock_in_time') and attendance.get('clock_out_time'):
        clock_out = parse_time_for_date(attendance.get('date'), attendance.get('clock_out_time'))
        if clock_in and clock_out:
            gross_seconds = max(0, int((clock_out - clock_in).total_seconds()))

    total_break_seconds = completed_seconds + active_seconds
    net_seconds = max(0, gross_seconds - total_break_seconds)
    active_break = next((b for b in scoped_breaks if not b.get('break_end')), None)

    attendance.update({
        'break_count': str(len([b for b in scoped_breaks if b.get('break_end')])),
        'total_break_time': format_duration(completed_seconds),
        'total_break_time_live': format_duration(total_break_seconds),
        'total_working_time': format_duration(gross_seconds) if gross_seconds else attendance.get('total_working_time', '00:00:00'),
        'net_working_time': format_duration(net_seconds),
        'late_display': format_minutes_display(attendance.get('late_minutes')),
        'overtime_display': format_hours_display(attendance.get('extra_hours')),
        'active_break_elapsed': format_duration(active_seconds),
        'on_break': 'Yes' if active_break else 'No',
        'current_break_id': active_break.get('break_id', '') if active_break else '',
        'breaks': [serialize_break(b, now) for b in scoped_breaks],
    })
    return attendance


def get_active_attendance(records, employee_id, today):
    return next((r for r in records if r['employee_id'] == employee_id and r['date'] == today and r['status'] == 'Active'), None)


def normalize_ip(ip):
    if not ip:
        return ''
    try:
        addr = ipaddress.ip_address(ip.strip())
        if addr.version == 6 and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return addr.compressed
    except ValueError:
        return ip.strip()


def get_local_ip_addresses():
    ips = set()
    try:
        hostname = socket.gethostname()
        for result in socket.getaddrinfo(hostname, None):
            family, _, _, _, sockaddr = result
            if family == socket.AF_INET:
                ips.add(sockaddr[0])
            elif family == socket.AF_INET6:
                addr = sockaddr[0].split('%')[0]
                ips.add(addr)
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))
            ips.add(sock.getsockname()[0])
    except Exception:
        pass
    return {normalize_ip(ip) for ip in ips if ip}


def get_ip():
    remote_addr = normalize_ip(request.remote_addr)
    xff = request.headers.get('X-Forwarded-For')
    xri = request.headers.get('X-Real-IP')
    if xff and (is_vercel_environment() or PUBLIC_DEPLOYMENT or (remote_addr in TRUSTED_PROXY_IPS)):
        return normalize_ip(xff.split(',')[0].strip())
    if xri and (is_vercel_environment() or PUBLIC_DEPLOYMENT or (remote_addr in TRUSTED_PROXY_IPS)):
        return normalize_ip(xri.strip())
    return remote_addr or ''

def is_ip_in_allowed_ranges(ip):
    ip = normalize_ip(ip)
    if not ip:
        return False
    for allowed in OFFICE_ALLOWED_IPS:
        if not allowed:
            continue
        allowed = allowed.strip()
        if allowed == '*':
            return True
        if allowed == ip:
            return True
        if allowed.endswith('.*'):
            prefix = allowed[:-2]
            if ip.startswith(prefix + '.'):
                return True
        if '/' in allowed:
            try:
                network = ipaddress.ip_network(allowed, strict=False)
                if ipaddress.ip_address(ip) in network:
                    return True
            except ValueError:
                continue
    return False


def ip_ok(ip):
    if is_demo_mode():
        return True
    if not ip:
        return False
    ip = normalize_ip(ip)
    if not ip:
        return False
    if ip in LOOPBACK_IPS:
        if allow_loopback() and not (is_public_deployment() or is_vercel_environment()):
            return True
        if is_public_deployment() or is_vercel_environment():
            return False
        for local_ip in get_local_ip_addresses():
            if is_ip_in_allowed_ranges(local_ip):
                return True
        return False
    return is_ip_in_allowed_ranges(ip)

def role_required(role):
    def dec(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'role' not in session:
                return redirect(url_for('login'))
            if session['role'] != role:
                return redirect(url_for('admin' if session['role']=='admin' else 'dashboard'))
            return f(*args, **kwargs)
        return wrapper
    return dec

def calc_late_extra(clock_in_str, clock_out_str, date_str, shift_start=None, shift_end=None):
    """Calculate late minutes and overtime against the employee's assigned shift."""
    late_minutes = 0
    extra_hours = 0.0
    shift_start = parse_hhmm(shift_start, DEFAULT_SHIFT_START)
    shift_end = parse_hhmm(shift_end, DEFAULT_SHIFT_END)
    work_start = datetime.strptime(date_str + ' ' + shift_start + ':00', '%Y-%m-%d %H:%M:%S')
    work_end = datetime.strptime(date_str + ' ' + shift_end + ':00', '%Y-%m-%d %H:%M:%S')
    if work_end <= work_start:
        work_end += timedelta(days=1)
    cin = datetime.strptime(date_str + ' ' + clock_in_str, '%Y-%m-%d %H:%M:%S')
    if cin > work_start:
        late_minutes = int((cin - work_start).total_seconds() / 60)
    if clock_out_str:
        cout = datetime.strptime(date_str + ' ' + clock_out_str, '%Y-%m-%d %H:%M:%S')
        if cout < cin:
            cout += timedelta(days=1)
        if cout > work_end:
            extra_hours = round((cout - work_end).total_seconds() / 3600, 2)
    return late_minutes, extra_hours


def calc_early_departure(clock_out_str, date_str, shift_start=None, shift_end=None):
    if not clock_out_str:
        return 0
    shift_start = parse_hhmm(shift_start, DEFAULT_SHIFT_START)
    shift_end = parse_hhmm(shift_end, DEFAULT_SHIFT_END)
    work_start = datetime.strptime(date_str + ' ' + shift_start + ':00', '%Y-%m-%d %H:%M:%S')
    work_end = datetime.strptime(date_str + ' ' + shift_end + ':00', '%Y-%m-%d %H:%M:%S')
    if work_end <= work_start:
        work_end += timedelta(days=1)
    cout = datetime.strptime(date_str + ' ' + clock_out_str, '%Y-%m-%d %H:%M:%S')
    if cout < work_start:
        cout += timedelta(days=1)
    if cout < work_end:
        return int((work_end - cout).total_seconds() / 60)
    return 0

@app.before_request
def enforce_office_network():
    if request.endpoint in ('static', 'access_denied_page', 'index', 'login'):
        return
    if is_demo_mode():
        return
    if 'role' not in session:
        return
    client_ip = get_ip()
    session_employee_id = session.get('employee_id')
    allowed = employee_ip_ok(session_employee_id, client_ip) if session.get('role') == 'employee' else ip_ok(client_ip)
    if not allowed:
        employee_id = session.get('employee_id') or session.get('admin_id') or request.form.get('employee_id') or 'unknown'
        action = request.endpoint or request.path
        log_denied_attempt(employee_id, action, client_ip, 'IP not in allowed list')
        session.clear()
        app.logger.warning('Access denied: remote_addr=%s xff=%s detected_ip=%s allowed=%s',
            request.remote_addr,
            request.headers.get('X-Forwarded-For'),
            client_ip,
            ','.join(OFFICE_ALLOWED_IPS)
        )
        return redirect(url_for('access_denied_page', ip=client_ip))

@app.route('/access-denied')
def access_denied_page():
    detected_ip = request.args.get('ip') or get_ip()
    allowed_ranges = ', '.join(OFFICE_ALLOWED_IPS)
    return render_template(
        'access_denied.html',
        detected_ip=detected_ip,
        allowed_ranges=allowed_ranges,
        message=ACCESS_DENIED_MESSAGE,
    ), 403

# Init CSVs
ensure_csv(EMPLOYEES_CSV, EMP_HEADERS)
ensure_csv(ATTENDANCE_CSV, ATT_HEADERS)
ensure_csv(DENIED_CSV, DENIED_HEADERS)
ensure_csv(LOGIN_LOGS_CSV, LOGIN_LOG_HEADERS)
ensure_csv(OFFICES_CSV, OFFICE_HEADERS)
ensure_csv(BREAKS_CSV, BREAK_HEADERS)

# Seed offices
if not read_csv(OFFICES_CSV):
    with open(get_storage_path(OFFICES_CSV), 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['OFF001', 'Head Office', 'Mumbai'])
        w.writerow(['OFF002', 'Branch Office', 'Delhi'])
        w.writerow(['OFF003', 'South Office', 'Bangalore'])

# Seed employees (with office column)
if not read_csv(EMPLOYEES_CSV):
    with open(get_storage_path(EMPLOYEES_CSV), 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['101', 'John Smith', 'Recruiting', 'Head Office', 'pass101', '101', DEFAULT_SHIFT_START, DEFAULT_SHIFT_END, '', 'Yes'])
        w.writerow(['102', 'Aisha Khan', 'Sales', 'Branch Office', 'pass102', '102', DEFAULT_SHIFT_START, DEFAULT_SHIFT_END, '', 'Yes'])
        w.writerow(['103', 'Ravi Patel', 'Operations', 'South Office', 'pass103', '103', DEFAULT_SHIFT_START, DEFAULT_SHIFT_END, '', 'Yes'])
else:
    emps = read_employees()
    current_emps = [coerce_row(EMP_HEADERS, row) for row in read_csv(EMPLOYEES_CSV)]
    if emps != current_emps:
        write_employees(emps)

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if 'role' in session:
        return redirect(url_for('admin' if session['role']=='admin' else 'dashboard'))
    if request.method == 'POST':
        ip = get_ip()
        lt = request.form.get('login_type')
        if lt == 'admin':
            attempted_admin_id = request.form.get('admin_id', '').strip()
            if attempted_admin_id==ADMIN_ID and request.form.get('admin_password')==ADMIN_PASSWORD:
                now = app_now()
                login_time = now.strftime('%Y-%m-%d %H:%M:%S')
                if not ip_ok(ip):
                    log_login(ADMIN_ID, 'admin', ip, 'denied-network')
                    log_denied_attempt(ADMIN_ID, 'login', ip, 'IP not in allowed list')
                    session.clear()
                    return redirect(url_for('access_denied_page', ip=ip))
                log_login(ADMIN_ID, 'admin', ip)
                session.update({'role':'admin','admin_id':ADMIN_ID,'login_time':login_time,'login_ip':ip})
                return redirect(url_for('admin'))
            log_login(attempted_admin_id or 'unknown', 'admin', ip, 'failed-invalid-credentials')
            return render_template('login.html', error_admin='Invalid admin credentials.', demo=DEMO_MODE)
        else:
            eid = request.form.get('employee_id','').strip()
            epw = request.form.get('employee_password','').strip()
            emp = next((e for e in read_employees() if e['employee_id']==eid and e['password']==epw), None)
            if emp:
                if emp.get('enabled') == 'No':
                    log_login(emp['employee_id'], 'employee', ip, 'disabled')
                    return render_template('login.html', error_emp='This employee account is disabled. Contact your administrator.', demo=DEMO_MODE)
                now = app_now()
                login_time = now.strftime('%Y-%m-%d %H:%M:%S')
                if not employee_ip_ok(emp['employee_id'], ip):
                    log_login(emp['employee_id'], 'employee', ip, 'denied-network')
                    log_denied_attempt(emp['employee_id'], 'login', ip, 'IP not in allowed list')
                    session.clear()
                    return redirect(url_for('access_denied_page', ip=ip))
                log_login(emp['employee_id'], 'employee', ip)
                session.update({'role':'employee','employee_id':emp['employee_id'],
                    'employee_name':emp['name'],'department':emp['department'],
                    'office':emp.get('office','Head Office'),
                    'login_time':login_time,'login_ip':ip})
                return redirect(url_for('dashboard'))
            log_login(eid or 'unknown', 'employee', ip, 'failed-invalid-credentials')
            return render_template('login.html', error_emp='Invalid Employee ID or password.', demo=DEMO_MODE)
    return render_template('login.html', demo=DEMO_MODE)

@app.route('/logout')
def logout():
    if 'role' in session:
        ip = get_ip()
        role = session.get('role')
        employee_id = session.get('employee_id') or session.get('admin_id') or 'unknown'
        log_activity(employee_id, role, 'Logout', ip)
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@role_required('employee')
def dashboard():
    records = read_attendance_records()
    breaks = read_break_records()
    current_time = app_now()
    today = current_time.strftime('%Y-%m-%d')
    eid = session['employee_id']
    employee = get_employee(eid) or {}
    active = get_active_attendance(records, eid, today)
    completed = next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Completed'), None)
    if active:
        active = serialize_attendance(active, breaks)
    if completed:
        completed = serialize_attendance(completed, breaks)
    status = 'On Break' if active and active.get('on_break') == 'Yes' else ('Clocked In' if active else ('Clocked Out' if completed else 'Not Clocked In'))
    active_break = None
    if active:
        active_break = next((b for b in breaks if b.get('attendance_id') == active.get('record_id') and not b.get('break_end')), None)

    # History: previous month only
    now = current_time.replace(tzinfo=None)
    first_this_month = now.replace(day=1)
    last_month_end = first_this_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    start_str = last_month_start.strftime('%Y-%m-%d')
    end_str = last_month_end.strftime('%Y-%m-%d')
    history = [serialize_attendance(r, breaks) for r in records if r['employee_id']==eid and start_str <= r['date'] <= end_str]
    history.sort(key=lambda x: x['date'], reverse=True)

    today_breaks = [serialize_break(b) for b in breaks if b['employee_id'] == eid and b['date'] == today]
    total_break_time = (active or completed or {}).get('total_break_time', '00:00:00')
    net_working_time = (active or completed or {}).get('net_working_time', '00:00:00')

    return render_template('index.html',
        name=session['employee_name'], department=session['department'],
        employee_id=session['employee_id'],
        office=session.get('office','Head Office'),
        status=status,
        today_record=active or completed, login_time=session.get('login_time'),
        demo=DEMO_MODE, today=current_time.strftime('%A, %d %B %Y'),
        now_time=current_time.strftime('%I:%M %p'),
        server_time_iso=iso_timestamp(current_time),
        shift_start=employee.get('shift_start', DEFAULT_SHIFT_START),
        shift_end=employee.get('shift_end', DEFAULT_SHIFT_END),
        history=history,
        history_month=last_month_end.strftime('%B %Y'),
        work_hours=WORK_HOURS,
        active_break=active_break,
        today_breaks=today_breaks,
        total_break_time=total_break_time,
        net_working_time=net_working_time,
        break_count=(active or completed or {}).get('break_count', '0'))

@app.route('/clock-in', methods=['POST'])
@role_required('employee')
def clock_in():
    try:
        ip = get_ip()
        if not employee_ip_ok(session['employee_id'], ip):
            log_denied_attempt(session['employee_id'], 'clock-in', ip, 'IP not in allowed list')
            return jsonify(success=False, message=ACCESS_DENIED_MESSAGE), 403
        eid, ename, dept = session['employee_id'], session['employee_name'], session['department']
        employee = get_employee(eid) or {}
        office = session.get('office', 'Head Office')
        now = app_now()
        today = now.strftime('%Y-%m-%d')
        if not employee_ip_ok(eid, ip):
            log_denied_attempt(eid, 'clock-in', ip, 'IP not in allowed list')
            return jsonify(success=False, message=ACCESS_DENIED_MESSAGE), 403
        with csv_lock(ATTENDANCE_CSV):
            records = read_attendance_records()
            if next((r for r in records if r['employee_id']==eid and r['date']==today), None):
                return jsonify(success=False, message='You already have an attendance record for today.')
            late_minutes, _ = calc_late_extra(now.strftime('%H:%M:%S'), '', today, employee.get('shift_start'), employee.get('shift_end'))
            late = late_minutes > 0
            new = {'record_id':next_numeric_id(records, 'record_id'),'employee_id':eid,'employee_name':ename,
                   'department':dept,'office':office,'date':today,
                   'clock_in_time':now.strftime('%H:%M:%S'),
                   'clock_out_time':'','total_hours':'',
                   'late_minutes':str(late_minutes),'extra_hours':'',
                   'clock_in_ip':ip,'clock_out_ip':'',
                   'status':'Active','late':'Yes' if late else 'No',
                   'total_break_time':'00:00:00','total_working_time':'00:00:00',
                   'net_working_time':'00:00:00','break_count':'0','on_break':'No','current_break_id':'',
                   'early_departure_minutes':'0'}
            records.append(new)
            write_attendance_records(records)
        log_activity(eid, 'employee', 'Clock In', ip, when=now)
        msg = f'Clock-in successful at {now.strftime("%I:%M %p")}.' + (f' Marked late ({late_minutes} min).' if late else '')
        return jsonify(success=True, message=msg, time=now.strftime('%H:%M:%S'), server_time_iso=iso_timestamp(now), late=late, late_minutes=late_minutes,
                       attendance=serialize_attendance(new, []))
    except Exception:
        app.logger.exception('Clock-in failed')
        return jsonify(success=False, message='Clock-in could not be saved. Please try again later.'), 500

@app.route('/clock-out', methods=['POST'])
@role_required('employee')
def clock_out():
    try:
        ip = get_ip()
        eid = session['employee_id']
        employee = get_employee(eid) or {}
        now = app_now()
        today = now.strftime('%Y-%m-%d')
        if not employee_ip_ok(eid, ip):
            log_denied_attempt(eid, 'clock-out', ip, 'IP not in allowed list')
            return jsonify(success=False, message=ACCESS_DENIED_MESSAGE), 403
        with csv_lock(ATTENDANCE_CSV), csv_lock(BREAKS_CSV):
            records = read_attendance_records()
            breaks = read_break_records()
            active = get_active_attendance(records, eid, today)
            if not active:
                return jsonify(success=False, message='No active clock-in found. Please clock in first.')
            active_break = next((b for b in breaks if b.get('attendance_id') == active.get('record_id') and not b.get('break_end')), None)
            if active.get('on_break') == 'Yes' or active_break:
                return jsonify(success=False, message='You must end your current break before Clocking Out.')
            cin = datetime.strptime(today+' '+active['clock_in_time'], '%Y-%m-%d %H:%M:%S')
            raw_seconds = int((now.replace(tzinfo=None)-cin).total_seconds())
            sync_break_totals(active, breaks)
            break_seconds = parse_duration_seconds(active.get('total_break_time'))
            net_seconds = max(0, raw_seconds - break_seconds)
            total = round(net_seconds / 3600, 2)
            late_minutes, shift_extra_hours = calc_late_extra(active['clock_in_time'], now.strftime('%H:%M:%S'), today, employee.get('shift_start'), employee.get('shift_end'))
            early_departure_minutes = calc_early_departure(now.strftime('%H:%M:%S'), today, employee.get('shift_start'), employee.get('shift_end'))
            extra_hours = shift_extra_hours
            active.update({
                'clock_out_time': now.strftime('%H:%M:%S'),
                'total_hours': str(total),
                'total_working_time': format_duration(raw_seconds),
                'net_working_time': format_duration(net_seconds),
                'late_minutes': str(late_minutes),
                'extra_hours': str(extra_hours),
                'early_departure_minutes': str(early_departure_minutes),
                'clock_out_ip': ip,
                'status': 'Completed'
            })
            write_attendance_records(records)
        log_activity(eid, 'employee', 'Clock Out', ip, when=now)
        extra_msg = f' | +{extra_hours}h overtime' if extra_hours > 0 else ''
        return jsonify(success=True, message=f'Clock-out at {now.strftime("%I:%M %p")}. Total: {total}h worked.{extra_msg}',
                       hours=total, extra_hours=extra_hours, time=now.strftime('%H:%M:%S'), server_time_iso=iso_timestamp(now),
                       attendance=serialize_attendance(active, breaks))
    except Exception:
        app.logger.exception('Clock-out failed')
        return jsonify(success=False, message='Clock-out could not be saved. Please try again later.'), 500

@app.route('/admin')
@role_required('admin')
def admin():
    records = read_attendance_records()
    breaks = read_break_records()
    denied = read_csv(DENIED_CSV)
    login_logs = read_csv(LOGIN_LOGS_CSV)
    employees = read_employees()
    offices = read_csv(OFFICES_CSV)
    current_time = app_now()
    today = current_time.strftime('%Y-%m-%d')
    week_start = (current_time-timedelta(days=current_time.weekday())).strftime('%Y-%m-%d')
    records = [serialize_attendance(r, breaks) for r in records]
    clocked_today = len([r for r in records if r['date']==today and r['status']=='Active'])
    missing = len([r for r in records if r['status']=='Active' and r['date']!=today])
    weekly_hrs = round(sum(float(r['total_hours']) for r in records if r.get('total_hours') and r['date']>=week_start), 2)
    denied_today = len([d for d in denied if d['timestamp'].startswith(today)])
    live = [r for r in records if r['date']==today and r['status']=='Active']

    # Enrich records with late/extra info display
    for r in records:
        if not r.get('late_minutes'):
            r['late_minutes'] = '0'
        if not r.get('extra_hours'):
            r['extra_hours'] = '0'
        if not r.get('office'):
            r['office'] = 'Head Office'

    office_names = [o['office_name'] for o in offices]

    return render_template('admin.html',
        records=sorted(records, key=lambda x: x['date'], reverse=True),
        denied=sorted(denied, key=lambda x: x['timestamp'], reverse=True),
        login_logs=sorted(login_logs, key=lambda x: x.get('timestamp', ''), reverse=True),
        clocked_today=clocked_today, missing=missing,
        weekly_hrs=weekly_hrs, denied_today=denied_today,
        employees=employees, live=live, demo=DEMO_MODE,
        offices=office_names,
        today=current_time.strftime('%A, %d %B %Y'),
        today_date=today,
        work_hours=WORK_HOURS,
        breaks=breaks)


@app.route('/admin/employees')
@role_required('admin')
def employee_management():
    employees = read_employees()
    offices = [o['office_name'] for o in read_csv(OFFICES_CSV)]
    return render_template(
        'employee_management.html',
        employees=employees,
        offices=offices,
        today=app_now().strftime('%A, %d %B %Y'),
        demo=DEMO_MODE,
    )


def employee_form_payload(existing_id=None):
    employee_id = request.form.get('employee_id', '').strip()
    name = request.form.get('name', '').strip()
    department = request.form.get('department', '').strip()
    office = request.form.get('office', '').strip() or 'Head Office'
    password = request.form.get('password', '').strip()
    username = request.form.get('username', '').strip() or employee_id
    shift_start = parse_hhmm(request.form.get('shift_start'), DEFAULT_SHIFT_START)
    shift_end = parse_hhmm(request.form.get('shift_end'), DEFAULT_SHIFT_END)
    allowed_ips = validate_ip_list(request.form.get('allowed_ips', ''))
    enabled = normalize_enabled(request.form.get('enabled', 'Yes'))
    final_employee_id = existing_id or employee_id
    if not final_employee_id or not name or not department or not password:
        raise ValueError('Employee ID, name, department, and password are required.')
    if shift_start == shift_end:
        raise ValueError('Shift start and shift end cannot be the same.')
    return {
        'employee_id': final_employee_id,
        'name': name,
        'department': department,
        'office': office,
        'password': password,
        'username': username,
        'shift_start': shift_start,
        'shift_end': shift_end,
        'allowed_ips': allowed_ips,
        'enabled': enabled,
    }


def redirect_admin_with_message(message, ok=True):
    return redirect(url_for('employee_management', employee_message=message, employee_status='ok' if ok else 'error'))


@app.route('/admin/employees/add', methods=['POST'])
@role_required('admin')
def add_employee():
    try:
        with csv_lock(EMPLOYEES_CSV):
            employees = read_employees()
            employee = employee_form_payload()
            if any(e['employee_id'].lower() == employee['employee_id'].lower() for e in employees):
                return redirect_admin_with_message('Employee ID already exists.', False)
            if any(e.get('username', '').lower() == employee['username'].lower() for e in employees):
                return redirect_admin_with_message('Username already exists.', False)
            employees.append(employee)
            write_employees(employees)
        return redirect_admin_with_message('Employee added successfully.')
    except ValueError as exc:
        return redirect_admin_with_message(str(exc), False)
    except Exception:
        app.logger.exception('Add employee failed')
        return redirect_admin_with_message('Employee could not be added. Please try again.', False)


@app.route('/admin/employees/<employee_id>/edit', methods=['POST'])
@role_required('admin')
def edit_employee(employee_id):
    try:
        with csv_lock(EMPLOYEES_CSV):
            employees = read_employees()
            idx = next((i for i, e in enumerate(employees) if e['employee_id'] == employee_id), None)
            if idx is None:
                return redirect_admin_with_message('Employee not found.', False)
            employee = employee_form_payload(existing_id=employee_id)
            if any(i != idx and e.get('username', '').lower() == employee['username'].lower() for i, e in enumerate(employees)):
                return redirect_admin_with_message('Username already exists.', False)
            employees[idx] = employee
            write_employees(employees)
        if session.get('employee_id') == employee_id:
            session.update({'employee_name': employee['name'], 'department': employee['department'], 'office': employee['office']})
        return redirect_admin_with_message('Employee updated successfully.')
    except ValueError as exc:
        return redirect_admin_with_message(str(exc), False)
    except Exception:
        app.logger.exception('Edit employee failed')
        return redirect_admin_with_message('Employee could not be updated. Please try again.', False)


@app.route('/admin/employees/<employee_id>/delete', methods=['POST'])
@role_required('admin')
def delete_employee(employee_id):
    try:
        with csv_lock(EMPLOYEES_CSV):
            employees = read_employees()
            remaining = [e for e in employees if e['employee_id'] != employee_id]
            if len(remaining) == len(employees):
                return redirect_admin_with_message('Employee not found.', False)
            write_employees(remaining)
        return redirect_admin_with_message('Employee deleted successfully.')
    except Exception:
        app.logger.exception('Delete employee failed')
        return redirect_admin_with_message('Employee could not be deleted. Please try again.', False)

@app.route('/break/start', methods=['POST'])
@role_required('employee')
def start_break():
    try:
        ip = get_ip()
        eid = session['employee_id']
        if not employee_ip_ok(eid, ip):
            log_denied_attempt(eid, 'break-in', ip, 'IP not in allowed list')
            return jsonify(success=False, message=ACCESS_DENIED_MESSAGE), 403
        now = app_now()
        today = now.strftime('%Y-%m-%d')
        with csv_lock(ATTENDANCE_CSV), csv_lock(BREAKS_CSV):
            records = read_attendance_records()
            breaks = read_break_records()
            active = get_active_attendance(records, eid, today)
            if not active:
                return jsonify(success=False, message='You must clock in before starting a break.')
            if active.get('on_break') == 'Yes':
                return jsonify(success=False, message='You are already on a break.')
            existing_break = next((b for b in breaks if b.get('attendance_id') == active.get('record_id') and not b.get('break_end')), None)
            if existing_break:
                active['on_break'] = 'Yes'
                active['current_break_id'] = existing_break.get('break_id', '')
                write_attendance_records(records)
                return jsonify(success=False, message='You are already on a break.'), 409
            break_id = next_numeric_id(breaks, 'break_id')
            breaks.append({
                'break_id': break_id,
                'attendance_id': active['record_id'],
                'employee_id': eid,
                'date': today,
                'break_start': now.strftime('%H:%M:%S'),
                'break_end': '',
                'duration_seconds': '0',
                'duration_display': '00:00:00',
                'break_start_ip': ip,
                'break_end_ip': ''
            })
            active['on_break'] = 'Yes'
            active['current_break_id'] = break_id
            write_break_records(breaks)
            write_attendance_records(records)
        log_activity(eid, 'employee', 'Break In', ip, when=now)
        return jsonify(success=True, message=f'Break started at {now.strftime("%I:%M %p")}.', break_id=break_id,
                       time=now.strftime('%H:%M:%S'), server_time_iso=iso_timestamp(now),
                       attendance=serialize_attendance(active, breaks))
    except Exception:
        app.logger.exception('Start break failed')
        return jsonify(success=False, message='Break could not be started. Please try again.'), 500


@app.route('/break/end', methods=['POST'])
@role_required('employee')
def end_break():
    try:
        ip = get_ip()
        eid = session['employee_id']
        if not employee_ip_ok(eid, ip):
            log_denied_attempt(eid, 'break-out', ip, 'IP not in allowed list')
            return jsonify(success=False, message=ACCESS_DENIED_MESSAGE), 403
        now = app_now()
        today = now.strftime('%Y-%m-%d')
        with csv_lock(ATTENDANCE_CSV), csv_lock(BREAKS_CSV):
            records = read_attendance_records()
            breaks = read_break_records()
            active = get_active_attendance(records, eid, today)
            if not active:
                return jsonify(success=False, message='No active attendance found.')
            active_break = next((b for b in breaks if b.get('attendance_id') == active.get('record_id') and not b.get('break_end')), None)
            if not active_break:
                return jsonify(success=False, message='There is no active break to end.')
            started = datetime.strptime(today + ' ' + active_break['break_start'], '%Y-%m-%d %H:%M:%S')
            duration_seconds = int((now.replace(tzinfo=None) - started).total_seconds())
            active_break['break_end'] = now.strftime('%H:%M:%S')
            active_break['duration_seconds'] = str(duration_seconds)
            active_break['duration_display'] = format_duration(duration_seconds)
            active_break['break_end_ip'] = ip
            sync_break_totals(active, breaks)
            active['on_break'] = 'No'
            active['current_break_id'] = ''
            write_break_records(breaks)
            write_attendance_records(records)
        log_activity(eid, 'employee', 'Break Out', ip, when=now)
        return jsonify(success=True, message=f'Break ended after {format_duration(duration_seconds)}.', break_duration=format_duration(duration_seconds),
                       time=now.strftime('%H:%M:%S'), server_time_iso=iso_timestamp(now),
                       attendance=serialize_attendance(active, breaks))
    except Exception:
        app.logger.exception('End break failed')
        return jsonify(success=False, message='Break could not be ended. Please try again.'), 500


@app.route('/attendance')
def attendance_summary():
    if 'role' not in session:
        return redirect(url_for('login'))
    records = read_attendance_records()
    breaks = read_break_records()
    today = app_now().strftime('%Y-%m-%d')
    if session['role'] == 'admin':
        serialized = [serialize_attendance(r, breaks) for r in records]
        serialized.sort(key=lambda x: (x.get('date', ''), x.get('clock_in_time', '')), reverse=True)
        return jsonify(success=True, attendance=serialized)

    eid = session['employee_id']
    active = get_active_attendance(records, eid, today)
    completed = next((r for r in records if r['employee_id'] == eid and r['date'] == today and r['status'] == 'Completed'), None)
    current = active or completed
    if current:
        current = serialize_attendance(current, breaks)
    return jsonify(
        success=True,
        clock_in=current.get('clock_in_time') if current else None,
        clock_out=current.get('clock_out_time') if current else None,
        total_break_time=current.get('total_break_time') if current else '00:00:00',
        total_break_time_live=current.get('total_break_time_live') if current else '00:00:00',
        net_working_time=current.get('net_working_time') if current else '00:00:00',
        break_count=current.get('break_count') if current else '0',
        status='On Break' if current and current.get('on_break') == 'Yes' else ('Clocked In' if active else ('Clocked Out' if completed else 'Not Clocked In')),
        attendance=current,
        breaks=current.get('breaks', []) if current else []
    )


@app.route('/api/time')
def api_time():
    now = app_now()
    return jsonify(
        iso=iso_timestamp(now),
        date=now.strftime('%Y-%m-%d'),
        time=now.strftime('%H:%M:%S'),
        display=now.strftime('%I:%M:%S %p'),
        timezone=now.tzname() or '',
        offset_seconds=int(now.utcoffset().total_seconds()) if now.utcoffset() else 0,
    )


@app.route('/attendance/<attendance_id>')
def attendance_details(attendance_id):
    if 'role' not in session:
        return redirect(url_for('login'))
    records = read_attendance_records()
    attendance = next((r for r in records if r['record_id'] == attendance_id), None)
    if not attendance:
        return jsonify(success=False, message='Attendance record not found.'), 404
    if session['role'] == 'employee' and attendance.get('employee_id') != session.get('employee_id'):
        return jsonify(success=False, message='You do not have access to that record.'), 403
    all_breaks = read_break_records()
    attendance = serialize_attendance(attendance, all_breaks)
    return jsonify(success=True, attendance=attendance, breaks=attendance.get('breaks', []))


@app.route('/report/weekly')
@role_required('admin')
def weekly_report():
    records = read_attendance_records()
    now = app_now()
    week_start = request.args.get('week_start', (now-timedelta(days=now.weekday())).strftime('%Y-%m-%d'))
    office_filter = request.args.get('office', '')
    week_end = (datetime.strptime(week_start,'%Y-%m-%d')+timedelta(days=6)).strftime('%Y-%m-%d')
    emp_data = {}
    for r in records:
        if r['date']<week_start or r['date']>week_end: continue
        if office_filter and r.get('office','Head Office') != office_filter: continue
        eid = r['employee_id']
        if eid not in emp_data:
            emp_data[eid]={'employee_id':eid,'employee_name':r['employee_name'],
                           'department':r['department'],'office':r.get('office','Head Office'),
                           'days':set(),'total_hours':0.0,'missing':0,'late_days':0,'extra_hours':0.0}
        emp_data[eid]['days'].add(r['date'])
        if r['status']=='Active': emp_data[eid]['missing']+=1
        if r.get('total_hours'): emp_data[eid]['total_hours']+=float(r['total_hours'])
        if r.get('late')=='Yes': emp_data[eid]['late_days']+=1
        if r.get('extra_hours'): emp_data[eid]['extra_hours']+=float(r['extra_hours'])
    report=[]
    for d in emp_data.values():
        days=len(d['days'])
        report.append({'employee_id':d['employee_id'],'employee_name':d['employee_name'],
            'department':d['department'],'office':d['office'],
            'week_start':week_start,'week_end':week_end,
            'days_worked':days,'total_hours':round(d['total_hours'],2),
            'average_hours_per_day':round(d['total_hours']/days,2) if days else 0,
            'missing_clockouts':d['missing'],
            'late_days':d['late_days'],
            'extra_hours':round(d['extra_hours'],2)})
    return jsonify(report=report, week_start=week_start, week_end=week_end)

@app.route('/report/download')
@role_required('admin')
def download_report():
    records = read_attendance_records()
    now = app_now()
    week_start = (now-timedelta(days=now.weekday())).strftime('%Y-%m-%d')
    week_end = (datetime.strptime(week_start,'%Y-%m-%d')+timedelta(days=6)).strftime('%Y-%m-%d')
    emp_data={}
    for r in records:
        if r['date']<week_start or r['date']>week_end: continue
        eid=r['employee_id']
        if eid not in emp_data:
            emp_data[eid]={'employee_id':eid,'employee_name':r['employee_name'],
                           'department':r['department'],'office':r.get('office','Head Office'),
                           'days':set(),'total_hours':0.0,'missing':0,'late_days':0,'extra_hours':0.0}
        emp_data[eid]['days'].add(r['date'])
        if r['status']=='Active': emp_data[eid]['missing']+=1
        if r.get('total_hours'): emp_data[eid]['total_hours']+=float(r['total_hours'])
        if r.get('late')=='Yes': emp_data[eid]['late_days']+=1
        if r.get('extra_hours'): emp_data[eid]['extra_hours']+=float(r['extra_hours'])
    out=io.StringIO()
    hdrs=['employee_id','employee_name','department','office','week_start','week_end',
          'days_worked','total_hours','average_hours_per_day','missing_clockouts','late_days','extra_hours']
    w=csv.DictWriter(out,fieldnames=hdrs); w.writeheader()
    for d in emp_data.values():
        days=len(d['days'])
        w.writerow({'employee_id':d['employee_id'],'employee_name':d['employee_name'],
            'department':d['department'],'office':d['office'],
            'week_start':week_start,'week_end':week_end,'days_worked':days,
            'total_hours':round(d['total_hours'],2),
            'average_hours_per_day':round(d['total_hours']/days,2) if days else 0,
            'missing_clockouts':d['missing'],'late_days':d['late_days'],
            'extra_hours':round(d['extra_hours'],2)})
    out.seek(0)
    return send_file(io.BytesIO(out.getvalue().encode()), mimetype='text/csv', as_attachment=True, download_name='weekly_report.csv')

@app.route('/api/status')
@role_required('employee')
def api_status():
    records=read_attendance_records()
    today=app_now().strftime('%Y-%m-%d')
    eid=session['employee_id']
    active=next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Active'),None)
    completed=next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Completed'),None)
    if active: return jsonify(status='Clocked In',clock_in_time=active['clock_in_time'])
    if completed: return jsonify(status='Clocked Out',total_hours=completed['total_hours'])
    return jsonify(status='Not Clocked In')

if __name__=='__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)

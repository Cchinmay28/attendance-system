from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import csv, ipaddress, os, socket, tempfile
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
OFFICE_ALLOWED_IPS = [x.strip() for x in os.getenv('OFFICE_ALLOWED_IPS', '192.168.0.0/24').split(',') if x.strip()]
LOOPBACK_IPS = {'127.0.0.1', '::1'}
OFFICE_ALLOWED_IPS = [x for x in OFFICE_ALLOWED_IPS if x not in LOOPBACK_IPS]
OFFICE_ALLOWED_IPS = OFFICE_ALLOWED_IPS or ['192.168.0.0/24']
TRUSTED_PROXY_IPS = [x.strip() for x in os.getenv('TRUSTED_PROXY_IPS', '').split(',') if x.strip()]
TRUSTED_PROXY_IPS = TRUSTED_PROXY_IPS or []
DEMO_MODE = os.getenv('DEMO_MODE', 'false').lower() == 'true'
PUBLIC_DEPLOYMENT = os.getenv('PUBLIC_DEPLOYMENT', 'false').lower() in ('1', 'true', 'yes')
ALLOW_LOOPBACK = os.getenv('ALLOW_LOCALHOST', 'false').lower() in ('1', 'true', 'yes')
WORK_HOURS = 8  # Company standard working hours
SUPABASE_URL = os.getenv('SUPABASE_URL', '').strip()
SUPABASE_KEY = (os.getenv('SUPABASE_SERVICE_ROLE_KEY') or os.getenv('SUPABASE_ANON_KEY') or os.getenv('SUPABASE_KEY', '')).strip()
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY and create_client)
SUPABASE_CLIENT = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_ENABLED else None

EMPLOYEES_CSV = 'employees.csv'
ATTENDANCE_CSV = 'attendance_records.csv'
DENIED_CSV = 'denied_attempts.csv'
OFFICES_CSV = 'offices.csv'
LOGIN_CSV = 'login_history.csv'
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
               'clock_in_ip','clock_out_ip',
               'break_start_time','break_end_time','break_minutes','break_start_ip','break_end_ip',
               'status','late']
EMP_HEADERS = ['employee_id','name','department','office','password']
DENIED_HEADERS = ['timestamp','employee_id','action','detected_ip','reason']
OFFICE_HEADERS = ['office_id','office_name','city']
LOGIN_HEADERS = ['login_id','employee_id','employee_name','role','login_time','ip']

def get_table_name(path):
    mapping = {
        'employees.csv': 'employees',
        'attendance_records.csv': 'attendance',
        'denied_attempts.csv': 'denied_attempts',
        'offices.csv': 'offices',
        'login_history.csv': 'login_history',
    }
    return mapping.get(os.path.basename(path), os.path.splitext(os.path.basename(path))[0])


def ensure_csv(path, headers):
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
                {'employee_id': '101', 'name': 'John Smith', 'department': 'Recruiting', 'office': 'Head Office', 'password': 'pass101'},
                {'employee_id': '102', 'name': 'Aisha Khan', 'department': 'Sales', 'office': 'Branch Office', 'password': 'pass102'},
                {'employee_id': '103', 'name': 'Ravi Patel', 'department': 'Operations', 'office': 'South Office', 'password': 'pass103'},
            ]
        else:
            seed_rows = []
        if seed_rows:
            SUPABASE_CLIENT.table(table_name).insert(seed_rows).execute()
        return

    storage_path = get_storage_path(os.path.basename(path))
    if not os.path.exists(storage_path):
        with open(storage_path, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(headers)


def read_csv(path):
    if SUPABASE_ENABLED:
        table_name = get_table_name(path)
        try:
            response = SUPABASE_CLIENT.table(table_name).select('*').execute()
            return response.data or []
        except Exception as exc:
            app.logger.warning('Supabase read failed for %s: %s', table_name, exc)
            return []

    storage_path = get_storage_path(os.path.basename(path))
    if not os.path.exists(storage_path):
        return []
    with open(storage_path, 'r', newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_csv(path, headers, rows):
    if SUPABASE_ENABLED:
        table_name = get_table_name(path)
        try:
            key_field = {
                'employees': 'employee_id',
                'attendance': 'record_id',
                'denied_attempts': 'timestamp',
                'offices': 'office_id',
            }.get(table_name)
            if key_field:
                SUPABASE_CLIENT.table(table_name).delete().neq(key_field, '').execute()
            else:
                SUPABASE_CLIENT.table(table_name).delete().execute()
            if rows:
                SUPABASE_CLIENT.table(table_name).insert(rows).execute()
            return
        except Exception as exc:
            app.logger.exception('Supabase write failed for %s', table_name)
            raise RuntimeError(f'Supabase write failed: {exc}') from exc

    storage_path = get_storage_path(os.path.basename(path))
    temp_path = None
    try:
        os.makedirs(os.path.dirname(storage_path), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix='attendance_', suffix='.tmp', dir=os.path.dirname(storage_path))
        with os.fdopen(fd, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            w.writerows(rows)
        os.replace(temp_path, storage_path)
    except Exception:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        raise

def log_login_event(employee_id, employee_name, role, ip):
    """Persist a successful login event together with the detected IP.
    Used for both admin and employee logins, to Supabase (if enabled) or CSV."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    row = {
        'login_id': f'{employee_id}-{now}',
        'employee_id': employee_id,
        'employee_name': employee_name,
        'role': role,
        'login_time': now,
        'ip': ip,
    }
    if SUPABASE_ENABLED:
        try:
            SUPABASE_CLIENT.table('login_history').insert(row).execute()
        except Exception:
            app.logger.exception('Failed to log login event to Supabase')
        return
    storage_path = get_storage_path(LOGIN_CSV)
    try:
        with open(storage_path, 'a', newline='', encoding='utf-8') as f:
            csv.DictWriter(f, fieldnames=LOGIN_HEADERS).writerow(row)
    except Exception:
        app.logger.exception('Failed to log login event to CSV')


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
    if DEMO_MODE:
        return True
    ip = normalize_ip(ip)
    if not ip:
        return False
    if ip in LOOPBACK_IPS:
        if ALLOW_LOOPBACK:
            return True
        if PUBLIC_DEPLOYMENT or is_vercel_environment():
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

def calc_late_extra(clock_in_str, clock_out_str, date_str):
    """Calculate late minutes and extra hours vs 8h workday."""
    late_minutes = 0
    extra_hours = 0.0
    work_start = datetime.strptime(date_str + ' 09:00:00', '%Y-%m-%d %H:%M:%S')
    cin = datetime.strptime(date_str + ' ' + clock_in_str, '%Y-%m-%d %H:%M:%S')
    if cin > work_start:
        late_minutes = int((cin - work_start).total_seconds() / 60)
    if clock_out_str:
        cout = datetime.strptime(date_str + ' ' + clock_out_str, '%Y-%m-%d %H:%M:%S')
        total_secs = (cout - cin).total_seconds()
        total_hours = total_secs / 3600
        expected_end = cin + timedelta(hours=WORK_HOURS)
        if cout > expected_end:
            extra_hours = round((cout - expected_end).total_seconds() / 3600, 2)
    return late_minutes, extra_hours

@app.before_request
def enforce_office_network():
    if request.endpoint in ('static', 'access_denied_page'):
        return
    if DEMO_MODE:
        return
    client_ip = get_ip()
    if not ip_ok(client_ip):
        app.logger.warning('Access denied: remote_addr=%s xff=%s detected_ip=%s allowed=%s',
            request.remote_addr,
            request.headers.get('X-Forwarded-For'),
            client_ip,
            ','.join(OFFICE_ALLOWED_IPS)
        )
        return redirect(url_for('access_denied_page'))

@app.route('/access-denied')
def access_denied_page():
    detected_ip = get_ip()
    allowed_ranges = ', '.join(OFFICE_ALLOWED_IPS)
    return app.response_class(
        response=f"""
        <!doctype html>
        <html lang='en'>
          <head><meta charset='utf-8'><title>Access Denied</title>
          <style>body{{font-family:Arial,sans-serif;padding:40px;background:#fff5f5;color:#7f1d1d;}} .box{{max-width:560px;margin:auto;padding:28px;border:1px solid #fecaca;border-radius:12px;background:#fff;}} h1{{margin-top:0;}} a{{color:#2563eb;}}</style></head>
          <body><div class='box'><h1>Access Denied</h1><p>This system is only accessible from the office network.</p><p>Your detected IP: <strong>{detected_ip or 'unknown'}</strong></p><p>Allowed network range: <strong>{allowed_ranges}</strong></p><p>Please connect to the office Wi-Fi or LAN and try again.</p><p><a href='/'>Go back to login</a></p></div></body>
        </html>
        """,
        status=403,
        mimetype='text/html'
    )

# Init CSVs
ensure_csv(EMPLOYEES_CSV, EMP_HEADERS)
ensure_csv(ATTENDANCE_CSV, ATT_HEADERS)
ensure_csv(DENIED_CSV, DENIED_HEADERS)
ensure_csv(OFFICES_CSV, OFFICE_HEADERS)
ensure_csv(LOGIN_CSV, LOGIN_HEADERS)

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
        w.writerow(['101', 'John Smith', 'Recruiting', 'Head Office', 'pass101'])
        w.writerow(['102', 'Aisha Khan', 'Sales', 'Branch Office', 'pass102'])
        w.writerow(['103', 'Ravi Patel', 'Operations', 'South Office', 'pass103'])
else:
    # Migrate existing employees to add office column if missing
    emps = read_csv(EMPLOYEES_CSV)
    changed = False
    for e in emps:
        if 'office' not in e or not e.get('office'):
            e['office'] = 'Head Office'
            changed = True
    if changed:
        write_csv(EMPLOYEES_CSV, EMP_HEADERS, emps)

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if 'role' in session:
        return redirect(url_for('admin' if session['role']=='admin' else 'dashboard'))
    if request.method == 'POST':
        lt = request.form.get('login_type')
        if lt == 'admin':
            if request.form.get('admin_id')==ADMIN_ID and request.form.get('admin_password')==ADMIN_PASSWORD:
                ip = get_ip()
                log_login_event(ADMIN_ID, 'Admin', 'admin', ip)
                session.update({'role':'admin','admin_id':ADMIN_ID,'login_time':datetime.now().strftime('%Y-%m-%d %H:%M:%S'),'login_ip':ip})
                return redirect(url_for('admin'))
            return render_template('login.html', error_admin='Invalid admin credentials.', demo=DEMO_MODE)
        else:
            eid = request.form.get('employee_id','').strip()
            epw = request.form.get('employee_password','').strip()
            emp = next((e for e in read_csv(EMPLOYEES_CSV) if e['employee_id']==eid and e['password']==epw), None)
            if emp:
                ip = get_ip()
                log_login_event(emp['employee_id'], emp['name'], 'employee', ip)
                session.update({'role':'employee','employee_id':emp['employee_id'],
                    'employee_name':emp['name'],'department':emp['department'],
                    'office':emp.get('office','Head Office'),
                    'login_time':datetime.now().strftime('%Y-%m-%d %H:%M:%S'),'login_ip':ip})
                return redirect(url_for('dashboard'))
            return render_template('login.html', error_emp='Invalid Employee ID or password.', demo=DEMO_MODE)
    return render_template('login.html', demo=DEMO_MODE)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@role_required('employee')
def dashboard():
    records = read_csv(ATTENDANCE_CSV)
    today = datetime.now().strftime('%Y-%m-%d')
    eid = session['employee_id']
    active = next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Active'), None)
    on_break = next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='OnBreak'), None)
    completed = next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Completed'), None)
    status = 'On Break' if on_break else ('Clocked In' if active else ('Clocked Out' if completed else 'Not Clocked In'))
    today_active_record = active or on_break

    # History: previous month only
    now = datetime.now()
    first_this_month = now.replace(day=1)
    last_month_end = first_this_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    start_str = last_month_start.strftime('%Y-%m-%d')
    end_str = last_month_end.strftime('%Y-%m-%d')
    history = [r for r in records if r['employee_id']==eid and start_str <= r['date'] <= end_str]
    history.sort(key=lambda x: x['date'], reverse=True)

    return render_template('index.html',
        name=session['employee_name'], department=session['department'],
        employee_id=session['employee_id'],
        office=session.get('office','Head Office'),
        status=status,
        today_record=today_active_record or completed, login_time=session.get('login_time'),
        demo=DEMO_MODE, today=datetime.now().strftime('%A, %d %B %Y'),
        now_time=datetime.now().strftime('%I:%M %p'),
        history=history,
        history_month=last_month_end.strftime('%B %Y'),
        work_hours=WORK_HOURS)

@app.route('/clock-in', methods=['POST'])
@role_required('employee')
def clock_in():
    try:
        ip = get_ip()
        eid, ename, dept = session['employee_id'], session['employee_name'], session['department']
        office = session.get('office', 'Head Office')
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        if not ip_ok(ip):
            with open(get_storage_path(DENIED_CSV), 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([now.strftime('%Y-%m-%d %H:%M:%S'),eid,'clock-in',ip,'IP not in allowed list'])
            return jsonify(success=False, message='Clock-in denied. You must be connected to the office network.')
        records = read_csv(ATTENDANCE_CSV)
        if next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Active'), None):
            return jsonify(success=False, message='You are already clocked in today.')
        late = now.hour > 9 or (now.hour==9 and now.minute>=5)
        late_minutes = max(0, int((now - now.replace(hour=9,minute=0,second=0,microsecond=0)).total_seconds() / 60)) if late else 0
        new = {'record_id':str(len(records)+1),'employee_id':eid,'employee_name':ename,
               'department':dept,'office':office,'date':today,
               'clock_in_time':now.strftime('%H:%M:%S'),
               'clock_out_time':'','total_hours':'',
               'late_minutes':str(late_minutes),'extra_hours':'',
               'clock_in_ip':ip,'clock_out_ip':'',
               'break_start_time':'','break_end_time':'','break_minutes':'0',
               'break_start_ip':'','break_end_ip':'',
               'status':'Active','late':'Yes' if late else 'No'}
        records.append(new)
        write_csv(ATTENDANCE_CSV, ATT_HEADERS, records)
        msg = f'Clock-in successful at {now.strftime("%I:%M %p")}.' + (f' ⚠ Marked Late ({late_minutes} min)' if late else '')
        return jsonify(success=True, message=msg, time=now.strftime('%H:%M:%S'), late=late, late_minutes=late_minutes)
    except Exception:
        app.logger.exception('Clock-in failed')
        return jsonify(success=False, message='Clock-in could not be saved. Please try again later.'), 500

@app.route('/start-break', methods=['POST'])
@role_required('employee')
def start_break():
    try:
        ip = get_ip()
        eid = session['employee_id']
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        if not ip_ok(ip):
            with open(get_storage_path(DENIED_CSV), 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([now.strftime('%Y-%m-%d %H:%M:%S'),eid,'start-break',ip,'IP not in allowed list'])
            return jsonify(success=False, message='Start-break denied. You must be connected to the office network.')
        records = read_csv(ATTENDANCE_CSV)
        active = next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Active'), None)
        if not active:
            on_break = next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='OnBreak'), None)
            if on_break:
                return jsonify(success=False, message='You are already on a break.')
            return jsonify(success=False, message='You must clock in before starting a break.')
        active.update({
            'break_start_time': now.strftime('%H:%M:%S'),
            'break_start_ip': ip,
            'status': 'OnBreak'
        })
        write_csv(ATTENDANCE_CSV, ATT_HEADERS, records)
        return jsonify(success=True, message=f'Break started at {now.strftime("%I:%M %p")}.', time=now.strftime('%H:%M:%S'))
    except Exception:
        app.logger.exception('Start-break failed')
        return jsonify(success=False, message='Start-break could not be saved. Please try again later.'), 500

@app.route('/end-break', methods=['POST'])
@role_required('employee')
def end_break():
    try:
        ip = get_ip()
        eid = session['employee_id']
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        if not ip_ok(ip):
            with open(get_storage_path(DENIED_CSV), 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([now.strftime('%Y-%m-%d %H:%M:%S'),eid,'end-break',ip,'IP not in allowed list'])
            return jsonify(success=False, message='End-break denied. You must be connected to the office network.')
        records = read_csv(ATTENDANCE_CSV)
        active = next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='OnBreak'), None)
        if not active:
            return jsonify(success=False, message='No active break found. Please start a break first.')
        bstart = datetime.strptime(today+' '+active['break_start_time'], '%Y-%m-%d %H:%M:%S')
        break_minutes = max(0, int((now-bstart).total_seconds()/60))
        prior_minutes = int(active.get('break_minutes') or 0)
        active.update({
            'break_end_time': now.strftime('%H:%M:%S'),
            'break_end_ip': ip,
            'break_minutes': str(prior_minutes + break_minutes),
            'status': 'Active'
        })
        write_csv(ATTENDANCE_CSV, ATT_HEADERS, records)
        return jsonify(success=True, message=f'Break ended at {now.strftime("%I:%M %p")}. Break duration: {break_minutes} min.',
                       break_minutes=break_minutes)
    except Exception:
        app.logger.exception('End-break failed')
        return jsonify(success=False, message='End-break could not be saved. Please try again later.'), 500

@app.route('/clock-out', methods=['POST'])
@role_required('employee')
def clock_out():
    try:
        ip = get_ip()
        eid = session['employee_id']
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        if not ip_ok(ip):
            with open(get_storage_path(DENIED_CSV), 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([now.strftime('%Y-%m-%d %H:%M:%S'),eid,'clock-out',ip,'IP not in allowed list'])
            return jsonify(success=False, message='Clock-out denied. You must be connected to the office network.')
        records = read_csv(ATTENDANCE_CSV)
        active = next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Active'), None)
        if not active:
            on_break = next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='OnBreak'), None)
            if on_break:
                return jsonify(success=False, message='Please end your break before clocking out.')
            return jsonify(success=False, message='No active clock-in found. Please clock in first.')
        cin = datetime.strptime(today+' '+active['clock_in_time'], '%Y-%m-%d %H:%M:%S')
        break_minutes = int(active.get('break_minutes') or 0)
        raw_total = (now-cin).total_seconds()/3600
        total = round(raw_total - (break_minutes/60), 2)
        late_minutes, extra_hours = calc_late_extra(active['clock_in_time'], now.strftime('%H:%M:%S'), today)
        active.update({
            'clock_out_time': now.strftime('%H:%M:%S'),
            'total_hours': str(total),
            'late_minutes': str(late_minutes),
            'extra_hours': str(extra_hours),
            'clock_out_ip': ip,
            'status': 'Completed'
        })
        write_csv(ATTENDANCE_CSV, ATT_HEADERS, records)
        extra_msg = f' | +{extra_hours}h overtime' if extra_hours > 0 else ''
        break_msg = f' | Break: {break_minutes} min' if break_minutes > 0 else ''
        return jsonify(success=True, message=f'Clock-out at {now.strftime("%I:%M %p")}. Total: {total}h worked.{break_msg}{extra_msg}',
                       hours=total, extra_hours=extra_hours)
    except Exception:
        app.logger.exception('Clock-out failed')
        return jsonify(success=False, message='Clock-out could not be saved. Please try again later.'), 500

@app.route('/admin')
@role_required('admin')
def admin():
    records = read_csv(ATTENDANCE_CSV)
    denied = read_csv(DENIED_CSV)
    employees = read_csv(EMPLOYEES_CSV)
    offices = read_csv(OFFICES_CSV)
    today = datetime.now().strftime('%Y-%m-%d')
    week_start = (datetime.now()-timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
    clocked_today = len([r for r in records if r['date']==today and r['status'] in ('Active','OnBreak')])
    missing = len([r for r in records if r['status']=='Active' and r['date']!=today])
    weekly_hrs = round(sum(float(r['total_hours']) for r in records if r.get('total_hours') and r['date']>=week_start), 2)
    denied_today = len([d for d in denied if d['timestamp'].startswith(today)])
    live = [r for r in records if r['date']==today and r['status'] in ('Active','OnBreak')]

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
        clocked_today=clocked_today, missing=missing,
        weekly_hrs=weekly_hrs, denied_today=denied_today,
        employees=employees, live=live, demo=DEMO_MODE,
        offices=office_names,
        today=datetime.now().strftime('%A, %d %B %Y'),
        today_date=today,
        work_hours=WORK_HOURS)

@app.route('/report/weekly')
@role_required('admin')
def weekly_report():
    records = read_csv(ATTENDANCE_CSV)
    week_start = request.args.get('week_start', (datetime.now()-timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d'))
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
    records = read_csv(ATTENDANCE_CSV)
    week_start = (datetime.now()-timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
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
    records=read_csv(ATTENDANCE_CSV)
    today=datetime.now().strftime('%Y-%m-%d')
    eid=session['employee_id']
    active=next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Active'),None)
    on_break=next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='OnBreak'),None)
    completed=next((r for r in records if r['employee_id']==eid and r['date']==today and r['status']=='Completed'),None)
    if on_break: return jsonify(status='On Break',break_start_time=on_break['break_start_time'])
    if active: return jsonify(status='Clocked In',clock_in_time=active['clock_in_time'])
    if completed: return jsonify(status='Clocked Out',total_hours=completed['total_hours'])
    return jsonify(status='Not Clocked In')

if __name__=='__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)

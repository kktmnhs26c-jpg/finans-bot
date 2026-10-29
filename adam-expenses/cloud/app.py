"""Private shared expense tracker on Postgres, owner/editor and partner/viewer."""
import os
import re
import hmac
import json
import time
import secrets
from datetime import timedelta
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, request, session, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,
)
DB = os.environ.get("DATABASE_URL", "")
SETUP_CODE = os.environ.get("OWNER_SETUP_CODE", "")
_db_ready = False
_attempts = {}

@app.after_request
def privacy(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    return resp

@app.get("/")
def homepage():
    return send_from_directory(ROOT, "index.html")

@app.get("/app.js")
def js():
    return send_from_directory(ROOT, "app.js")

@app.get("/health")
def health():
    return jsonify(status="ok", database_configured=bool(DB), setup_configured=bool(SETUP_CODE))


def conn():
    if not DB:
        raise RuntimeError("Для запуска общего хранения нужно подключить PostgreSQL (DATABASE_URL).")
    return psycopg.connect(DB, row_factory=dict_row, connect_timeout=7, autocommit=False)


def ensure_schema():
    global _db_ready
    if _db_ready:
        return
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS expense_users (
                  username TEXT PRIMARY KEY CHECK (username IN ('owner', 'partner')),
                  password_hash TEXT NOT NULL,
                  scope TEXT NOT NULL DEFAULT 'all',
                  version INTEGER NOT NULL DEFAULT 1
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS expense_records (
                  id TEXT PRIMARY KEY,
                  amount BIGINT NOT NULL CHECK (amount > 0),
                  date TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  category TEXT NOT NULL,
                  payment TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )''')
        c.commit()
    _db_ready = True

@app.before_request
def api_guard():
    if not request.path.startswith('/api/'):
        return
    if not DB:
        return jsonify(error='Общая база данных ещё не подключена. Текущие расходы сохранены на прежнем сайте.'), 503
    try:
        ensure_schema()
    except (psycopg.Error, RuntimeError):
        return jsonify(error='Не удалось подключиться к общей базе данных.'), 503
    if request.method in ('POST','PUT','PATCH','DELETE'):
        if request.mimetype != 'application/json':
            return jsonify(error='Требуется JSON запрос.'), 415
        # No cross-origin mutations; browsers supply Origin on fetch POST.
        origin = request.headers.get('Origin')
        if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
            return jsonify(error='Запрос с другого сайта запрещён.'), 403


def input_json():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        raise ValueError('Неверные данные запроса')
    return d


def user_row():
    name = session.get('user')
    if name not in ('owner', 'partner'):
        return None
    with conn() as c:
        return c.execute('SELECT username, scope, version FROM expense_users WHERE username=%s', (name,)).fetchone()


def require_login(owner=False):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            user = user_row()
            if not user or session.get('version') != user['version']:
                session.clear()
                return jsonify(error='Войдите в аккаунт.'), 401
            if owner and user['username'] != 'owner':
                return jsonify(error='Только владелец может изменять расходы.'), 403
            return fn(user, *a, **kw)
        return wrapper
    return decorator


@app.get('/api/me')
def me():
    u = user_row()
    if not u or session.get('version') != u['version']:
        session.clear()
        with conn() as c:
            is_ready = bool(c.execute("SELECT 1 FROM expense_users WHERE username='owner'").fetchone())
        return jsonify(authenticated=False, needs_setup=not is_ready)
    return jsonify(authenticated=True, role=u['username'], scope=u['scope'])


@app.post('/api/setup')
def setup():
    try:
        p=input_json()
        password=p.get('password', '')
        provided=p.get('setupCode', '')
        if not SETUP_CODE or not isinstance(provided, str) or not hmac.compare_digest(provided, SETUP_CODE):
            return jsonify(error='Неверный код первоначальной настройки.'), 403
        if not isinstance(password, str) or len(password)<12 or len(password)>200:
            return jsonify(error='Пароль владельца должен содержать не менее 12 символов.'), 400
        with conn() as c:
            result=c.execute("INSERT INTO expense_users(username, password_hash, scope) VALUES('owner',%s,'all') ON CONFLICT DO NOTHING RETURNING version",(generate_password_hash(password),)).fetchone()
            c.commit()
        if not result:
            return jsonify(error='Владелец уже зарегистрирован.'), 409
        session.clear()
        session['user']='owner';session['version']=result['version'];session.permanent=True
        return jsonify(ok=True)
    except ValueError as err:
        return jsonify(error=str(err)),400


@app.post('/api/login')
def login():
    try:
        p=input_json();username=p.get('username');password=p.get('password')
        if username not in ('owner','partner') or not isinstance(password,str):
            return jsonify(error='Неверный логин или пароль.'),401
        ip=request.remote_addr or 'unknown'
        key=(ip,username)
        now=time.monotonic(); timestamps=[t for t in _attempts.get(key,[]) if now-t <900]
        if len(timestamps)>=12:
            return jsonify(error='Слишком много попыток. Повторите через 15 минут.'),429
        with conn() as c:
            row=c.execute('SELECT password_hash, version FROM expense_users WHERE username=%s',(username,)).fetchone()
        if not row or not check_password_hash(row['password_hash'],password):
            timestamps.append(now);_attempts[key]=timestamps
            return jsonify(error='Неверный логин или пароль.'),401
        _attempts.pop(key,None);session.clear();session['user']=username;session['version']=row['version'];session.permanent=True
        return jsonify(ok=True)
    except ValueError as e:
        return jsonify(error=str(e)),400


@app.post('/api/logout')
def logout():
    session.clear();return jsonify(ok=True)

@app.get('/api/expenses')
@require_login()
def list_expenses(user):
    with conn() as c:
        if user['username']=='partner' and user['scope']=='shop':
            rows=c.execute("SELECT id,amount,date,scope,category,payment,note FROM expense_records WHERE scope='Магазин' ORDER BY date DESC, created_at DESC").fetchall()
        else:
            rows=c.execute('SELECT id,amount,date,scope,category,payment,note FROM expense_records ORDER BY date DESC, created_at DESC').fetchall()
    return jsonify(expenses=rows)


def validate_record(r):
    if not isinstance(r,dict): raise ValueError('Неверный формат расхода.')
    id=r.get('id')
    amount=r.get('amount')
    date=r.get('date')
    scope=r.get('scope')
    cat=r.get('category')
    payment=r.get('payment')
    note=r.get('note','')
    if not isinstance(id,str) or not 1<=len(id)<=100 or not re.fullmatch(r'[a-zA-Z0-9_-]+',id):
        raise ValueError('Неверный идентификатор расхода.')
    if isinstance(amount,bool) or not isinstance(amount,int) or not 1<=amount<=100_000_000_000:
        raise ValueError('Неверная сумма расхода.')
    if not isinstance(date,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date):
        raise ValueError('Неверная дата расхода.')
    from datetime import date as dt_date
    if date.endswith('-00'):
        if not 1<=int(date[5:7])<=12:
            raise ValueError('Неверный месяц расхода.')
    else:
        try: dt_date.fromisoformat(date)
        except ValueError: raise ValueError('Неверная дата расхода.') from None
    if scope not in ('Магазин','Личные') or payment not in ('Карта','Наличные','Перевод','Не указано'):
        raise ValueError('Неверный раздел или способ оплаты.')
    if not isinstance(cat,str) or not 1<=len(cat.strip())<=60 or not isinstance(note,str) or len(note)>500:
        raise ValueError('Неверная категория или комментарий.')
    return (id,amount,date,scope,cat.strip(),payment,note)


@app.post('/api/expenses')
@require_login(owner=True)
def save_expense(user):
    try:
        vals=validate_record(input_json().get('expense'))
        with conn() as c:
            c.execute('''INSERT INTO expense_records(id,amount,date,scope,category,payment,note) VALUES(%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(id) DO UPDATE SET amount=EXCLUDED.amount,date=EXCLUDED.date,scope=EXCLUDED.scope,
              category=EXCLUDED.category,payment=EXCLUDED.payment,note=EXCLUDED.note,updated_at=NOW()''',vals)
            c.commit()
        return jsonify(ok=True)
    except ValueError as e:
        return jsonify(error=str(e)),400

@app.delete('/api/expenses/<record_id>')
@require_login(owner=True)
def delete_expense(user,record_id):
    with conn() as c:
        c.execute('DELETE FROM expense_records WHERE id=%s',(record_id,));c.commit()
    return jsonify(ok=True)

@app.post('/api/import')
@require_login(owner=True)
def import_expenses(user):
    try:
        items=input_json().get('expenses')
        if not isinstance(items,list) or len(items)>50000:
            raise ValueError('Ожидается список не более 50 000 расходов.')
        vals=[validate_record(x) for x in items]
        if len(set(x[0] for x in vals)) != len(vals): raise ValueError('Повторяющиеся идентификаторы в файле.')
        with conn() as c:
            old_ids=set()
            if vals:
                for row in c.execute('SELECT id FROM expense_records WHERE id=ANY(%s)',([v[0] for v in vals],)).fetchall():
                    old_ids.add(row['id'])
            added=0
            for v in vals:
                if v[0] not in old_ids:
                    c.execute('INSERT INTO expense_records(id,amount,date,scope,category,payment,note) VALUES(%s,%s,%s,%s,%s,%s,%s)',v)
                    added+=1
            c.commit()
        return jsonify(ok=True,added=added,duplicates=len(vals)-added)
    except ValueError as e:
        return jsonify(error=str(e)),400

@app.get('/api/export')
@require_login(owner=True)
def export_expenses(user):
    with conn() as c:
        rows=c.execute('SELECT id,amount,date,scope,category,payment,note FROM expense_records ORDER BY date DESC').fetchall()
    return jsonify(format='adam-expenses',version=1,expenses=rows)

@app.get('/api/partner')
@require_login(owner=True)
def partner_status(user):
    with conn() as c:
        row=c.execute("SELECT scope FROM expense_users WHERE username='partner'").fetchone()
    return jsonify(configured=bool(row),scope=row['scope'] if row else 'shop')

@app.put('/api/partner')
@require_login(owner=True)
def partner_settings(user):
    try:
        p=input_json();password=p.get('password');scope=p.get('scope','shop')
        if scope not in ('shop','all'): raise ValueError('Неверный режим доступа.')
        if not isinstance(password,str) or not 12<=len(password)<=200:
            raise ValueError('Пароль партнёра должен содержать не менее 12 символов.')
        with conn() as c:
            c.execute('''INSERT INTO expense_users(username,password_hash,scope,version) VALUES('partner',%s,%s,1)
                  ON CONFLICT(username) DO UPDATE SET password_hash=EXCLUDED.password_hash,scope=EXCLUDED.scope,version=expense_users.version+1''',(generate_password_hash(password),scope))
            c.commit()
        return jsonify(ok=True)
    except ValueError as e:
        return jsonify(error=str(e)),400

@app.errorhandler(413)
def too_large(e):return jsonify(error='Файл слишком большой (не более 6 МБ).'),413

if __name__ == '__main__':
    app.run(host='127.0.0.1',port=int(os.environ.get('PORT','5000')))
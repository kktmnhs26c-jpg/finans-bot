import os,re,secrets,hmac,time
from functools import wraps
from datetime import date,timedelta
import psycopg
from psycopg.rows import dict_row
from flask import Flask,request,jsonify,session,send_from_directory
from werkzeug.security import generate_password_hash,check_password_hash

app=Flask(__name__,static_folder=None)
app.config.update(SECRET_KEY=os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32),SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SECURE=True,SESSION_COOKIE_SAMESITE="Lax",PERMANENT_SESSION_LIFETIME=timedelta(days=7),MAX_CONTENT_LENGTH=6*1024*1024)
DATABASE_URL=os.environ.get("DATABASE_URL","")
OWNER_SETUP_CODE=os.environ.get("OWNER_SETUP_CODE","")
_initialized=False
_attempts={}

@app.after_request
def headers(r):
    r.headers["Cache-Control"]="no-store"
    r.headers["X-Frame-Options"]="DENY"
    r.headers["X-Content-Type-Options"]="nosniff"
    r.headers["Referrer-Policy"]="no-referrer"
    r.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
    return r

@app.get("/")
def root():return send_from_directory(os.path.dirname(__file__),"index.html")
@app.get("/app.js")
def js():return send_from_directory(os.path.dirname(__file__),"app.js")
@app.get("/health")
def health():return jsonify(ok=True,database_configured=bool(DATABASE_URL),setup_configured=bool(OWNER_SETUP_CODE))

def db():
    if not DATABASE_URL:raise RuntimeError("DB missing")
    return psycopg.connect(DATABASE_URL,row_factory=dict_row,connect_timeout=7)

def initialize():
    global _initialized
    if _initialized:return
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS expenses_cloud_users(username TEXT PRIMARY KEY,password_hash TEXT NOT NULL,scope TEXT NOT NULL DEFAULT 'all',version INTEGER NOT NULL DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS expenses_cloud_records(id TEXT PRIMARY KEY,amount BIGINT NOT NULL,date TEXT NOT NULL,scope TEXT NOT NULL,category TEXT NOT NULL,payment TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    _initialized=True

@app.before_request
def verify_request():
    if not request.path.startswith("/api/"):return
    if not DATABASE_URL:return jsonify(error="Общая база ещё не подключена. Прежний сайт работает отдельно."),503
    try:initialize()
    except Exception:return jsonify(error="Не удалось связаться с общей базой."),503
    if request.method in ("POST","PUT","DELETE"):
        if request.mimetype!="application/json":return jsonify(error="Требуется JSON."),415
        origin=request.headers.get("Origin")
        if origin and origin.rstrip("/")!=request.host_url.rstrip("/"):
            return jsonify(error="Запрос с другого сайта запрещён."),403

def body():
    b=request.get_json(silent=True)
    if not isinstance(b,dict):raise ValueError("Некорректный запрос")
    return b

def current_user():
    name=session.get("user")
    if name not in ("owner","partner"):return None
    with db() as c:
        row=c.execute("SELECT username,scope,version FROM expenses_cloud_users WHERE username=%s",(name,)).fetchone()
    return row if row and row["version"]==session.get("version") else None

def auth(owner=False):
    def decorate(func):
        @wraps(func)
        def wrap(*a,**kw):
            user=current_user()
            if not user:
                session.clear();return jsonify(error="Требуется вход."),401
            if owner and user["username"]!="owner":return jsonify(error="Партнёр может только просматривать расходы."),403
            return func(user,*a,**kw)
        return wrap
    return decorate

@app.get("/api/me")
def me():
    u=current_user()
    if u:return jsonify(authenticated=True,role=u["username"],scope=u["scope"])
    with db() as c:
        exists=bool(c.execute("SELECT 1 FROM expenses_cloud_users WHERE username='owner'").fetchone())
    session.clear()
    return jsonify(authenticated=False,needs_setup=not exists)

@app.post("/api/setup")
def setup():
    try:
        b=body();password=b.get("password");code=b.get("setupCode")
        if not OWNER_SETUP_CODE or not isinstance(code,str) or not hmac.compare_digest(code.encode('utf-8'),OWNER_SETUP_CODE.encode('utf-8')):return jsonify(error="Неверный код настройки."),403
        if not isinstance(password,str) or len(password)<12:return jsonify(error="Пароль от 12 символов."),400
        with db() as c:
            row=c.execute("INSERT INTO expenses_cloud_users(username,password_hash,scope) VALUES('owner',%s,'all') ON CONFLICT DO NOTHING RETURNING version",(generate_password_hash(password),)).fetchone()
        if not row:return jsonify(error="Владелец уже создан."),409
        session.clear();session["user"]="owner";session["version"]=row["version"];session.permanent=True
        return jsonify(ok=True)
    except ValueError as e:return jsonify(error=str(e)),400

@app.post("/api/login")
def login():
    try:
        b=body();name=b.get("username");pw=b.get("password")
        if name not in ("owner","partner") or not isinstance(pw,str):return jsonify(error="Неверный логин или пароль."),401
        key=(request.remote_addr,name);now=time.monotonic()
        history=[x for x in _attempts.get(key,[]) if now-x<900]
        if len(history)>=12:return jsonify(error="Слишком много попыток. Попробуйте позже."),429
        with db() as c:
            r=c.execute("SELECT password_hash,version FROM expenses_cloud_users WHERE username=%s",(name,)).fetchone()
        if not r or not check_password_hash(r["password_hash"],pw):
            history.append(now);_attempts[key]=history
            return jsonify(error="Неверный логин или пароль."),401
        _attempts.pop(key,None)
        session.clear();session["user"]=name;session["version"]=r["version"];session.permanent=True
        return jsonify(ok=True)
    except ValueError as e:return jsonify(error=str(e)),400

@app.post("/api/logout")
def logout():session.clear();return jsonify(ok=True)

def valid(r):
    if not isinstance(r,dict):raise ValueError("Операция должна быть объектом.")
    id=r.get("id");n=r.get("amount");d=r.get("date");scope=r.get("scope");category=r.get("category");payment=r.get("payment");note=r.get("note","")
    if not isinstance(id,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}",id):raise ValueError("Неверный ID.")
    if type(n)!=int or not 1<=n<=100_000_000_000:raise ValueError("Неверная сумма.")
    if not isinstance(d,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",d):raise ValueError("Неверная дата.")
    if d.endswith("-00"):
        if not 1<=int(d[5:7])<=12:raise ValueError("Неверный месяц.")
    else:
        try:date.fromisoformat(d)
        except ValueError:raise ValueError("Неверная дата.") from None
    if scope not in ("Личные","Магазин") or payment not in ("Карта","Наличные","Перевод","Не указано"):raise ValueError("Неверный раздел или оплата.")
    if not isinstance(category,str) or not 1<=len(category.strip())<=60 or not isinstance(note,str) or len(note)>500:raise ValueError("Неверная категория или заметка.")
    return (id,n,d,scope,category.strip(),payment,note)

@app.get("/api/expenses")
@auth()
def expenses(user):
    with db() as c:
        if user["username"]=="partner" and user["scope"]=="shop":
            rows=c.execute("SELECT id,amount,date,scope,category,payment,note FROM expenses_cloud_records WHERE scope='Магазин' ORDER BY date DESC,created_at DESC").fetchall()
        else:rows=c.execute("SELECT id,amount,date,scope,category,payment,note FROM expenses_cloud_records ORDER BY date DESC,created_at DESC").fetchall()
    return jsonify(expenses=rows)

@app.post("/api/expenses")
@auth(owner=True)
def save(user):
    try:
        v=valid(body().get("expense"))
        with db() as c:
            c.execute("""INSERT INTO expenses_cloud_records(id,amount,date,scope,category,payment,note) VALUES(%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET amount=EXCLUDED.amount,date=EXCLUDED.date,scope=EXCLUDED.scope,category=EXCLUDED.category,payment=EXCLUDED.payment,note=EXCLUDED.note""",v)
        return jsonify(ok=True)
    except ValueError as e:return jsonify(error=str(e)),400

@app.delete("/api/expenses/<id>")
@auth(owner=True)
def delete(user,id):
    with db() as c:c.execute("DELETE FROM expenses_cloud_records WHERE id=%s",(id,))
    return jsonify(ok=True)

@app.post("/api/import")
@auth(owner=True)
def import_records(user):
    try:
        items=body().get("expenses")
        if not isinstance(items,list) or len(items)>50000:raise ValueError("Нужен список не более 50000 расходов.")
        values=[valid(r) for r in items]
        if len({v[0] for v in values})!=len(values):raise ValueError("В файле повторяются ID.")
        count=0
        with db() as c:
            for v in values:
                if c.execute("INSERT INTO expenses_cloud_records(id,amount,date,scope,category,payment,note) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING RETURNING id",v).fetchone():count+=1
        return jsonify(ok=True,added=count,duplicates=len(values)-count)
    except ValueError as e:return jsonify(error=str(e)),400

@app.get("/api/export")
@auth(owner=True)
def export_records(user):
    with db() as c:rows=c.execute("SELECT id,amount,date,scope,category,payment,note FROM expenses_cloud_records ORDER BY date DESC").fetchall()
    return jsonify(format="adam-expenses",version=1,expenses=rows)

@app.get("/api/partner")
@auth(owner=True)
def get_partner(user):
    with db() as c:r=c.execute("SELECT scope FROM expenses_cloud_users WHERE username='partner'").fetchone()
    return jsonify(configured=bool(r),scope=r["scope"] if r else "all")

@app.put("/api/partner")
@auth(owner=True)
def put_partner(user):
    try:
        b=body();pw=b.get("password");scope=b.get("scope")
        if scope not in ("shop","all"):raise ValueError("Неверные права доступа.")
        if not isinstance(pw,str) or not 12<=len(pw)<=200:raise ValueError("Пароль партнёра от 12 символов.")
        with db() as c:
            c.execute("""INSERT INTO expenses_cloud_users(username,password_hash,scope,version) VALUES('partner',%s,%s,1)
              ON CONFLICT(username) DO UPDATE SET password_hash=EXCLUDED.password_hash,scope=EXCLUDED.scope,version=expenses_cloud_users.version+1""",(generate_password_hash(pw),scope))
        return jsonify(ok=True)
    except ValueError as e:return jsonify(error=str(e)),400


@app.put("/api/password")
@auth(owner=True)
def update_owner_password(user):
    try:
        b=body()
        old=b.get("currentPassword")
        fresh=b.get("newPassword")
        if not isinstance(old,str) or not isinstance(fresh,str) or not 12<=len(fresh)<=200:
            raise ValueError("Новый пароль должен содержать от 12 до 200 символов.")
        with db() as c:
            info=c.execute("SELECT password_hash,version FROM expenses_cloud_users WHERE username='owner'").fetchone()
            if not info or not check_password_hash(info["password_hash"],old):
                return jsonify(error="Текущий пароль неверный."),403
            updated=c.execute("UPDATE expenses_cloud_users SET password_hash=%s,version=version+1 WHERE username='owner' RETURNING version",(generate_password_hash(fresh),)).fetchone()
        session.clear()
        session["user"]="owner"
        session["version"]=updated["version"]
        session.permanent=True
        return jsonify(ok=True)
    except ValueError as e:
        return jsonify(error=str(e)),400

@app.errorhandler(413)
def big(e):return jsonify(error="Файл слишком большой."),413

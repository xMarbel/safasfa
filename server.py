"""VILO — local-first live chat server. Not audited for public Internet deployment."""
import os, time, json, secrets, hashlib, hmac, re, sqlite3, asyncio, threading, mimetypes, uuid, io
from PIL import Image, ImageOps, UnidentifiedImageError
from pathlib import Path
from contextlib import contextmanager, closing
from fastapi import FastAPI, Request, Response, HTTPException, Depends, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('VILO_DATA_DIR',ROOT/'data')).resolve()
DATA.mkdir(parents=True,exist_ok=True)
UPLOADS=DATA/'uploads'; UPLOADS.mkdir(exist_ok=True)
PROFILE_MEDIA=DATA/'profile_media'; PROFILE_MEDIA.mkdir(exist_ok=True)
DB_PATH=DATA/'vilo.db'
LOCK=threading.RLock()
app=FastAPI(title='VILO',docs_url=None,redoc_url=None,openapi_url=None)
# An instance runs with a single uvicorn worker.
connected:dict[int,set[WebSocket]]={}
rate:dict[str,list[float]]={}

def db():
    c=sqlite3.connect(DB_PATH,timeout=20,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA busy_timeout=20000')
    return c

@contextmanager
def transaction():
    with LOCK:
        c=db()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except Exception:
            c.rollback(); raise
        finally:
            c.close()

def one(query,*args):
    with closing(db()) as c: return c.execute(query,args).fetchone()
def allrows(query,*args):
    with closing(db()) as c: return [dict(r) for r in c.execute(query,args).fetchall()]

def init_db():
    with transaction() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, email TEXT NOT NULL UNIQUE COLLATE NOCASE, pw TEXT NOT NULL, display_name TEXT NOT NULL, about TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '#7c5cf0', created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms(id INTEGER PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('dm','group','server')),name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',owner_id INTEGER REFERENCES users(id),created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS members(room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,role TEXT NOT NULL DEFAULT 'member',joined INTEGER NOT NULL,PRIMARY KEY(room_id,user_id));
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id),bot_name TEXT,body TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'text',ref_id INTEGER,attachment_id TEXT,attachment_name TEXT,created INTEGER NOT NULL,edited INTEGER,deleted INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id,id);
        CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,emoji TEXT NOT NULL,PRIMARY KEY(message_id,user_id,emoji));
        CREATE TABLE IF NOT EXISTS friendships(id INTEGER PRIMARY KEY, sender INTEGER NOT NULL REFERENCES users(id),receiver INTEGER NOT NULL REFERENCES users(id),status TEXT NOT NULL DEFAULT 'pending',created INTEGER NOT NULL, UNIQUE(sender,receiver));
        CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,by_user INTEGER NOT NULL REFERENCES users(id),max_uses INTEGER NOT NULL DEFAULT 30,used INTEGER NOT NULL DEFAULT 0,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS polls(id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,question TEXT NOT NULL,creator INTEGER NOT NULL REFERENCES users(id),multiple INTEGER NOT NULL DEFAULT 0,anonymous INTEGER NOT NULL DEFAULT 0,created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS poll_options(id INTEGER PRIMARY KEY,poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,label TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS poll_votes(poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,option_id INTEGER NOT NULL REFERENCES poll_options(id),user_id INTEGER NOT NULL REFERENCES users(id),PRIMARY KEY(poll_id,option_id,user_id));
        CREATE TABLE IF NOT EXISTS bots(id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,owner_id INTEGER NOT NULL REFERENCES users(id),name TEXT NOT NULL,trigger TEXT NOT NULL,response TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,by_user INTEGER NOT NULL REFERENCES users(id),filename TEXT NOT NULL,stored TEXT NOT NULL,size INTEGER NOT NULL,created INTEGER NOT NULL);
        ''')
        # Compatible profile upgrade: do not delete existing users or messages.
        user_cols={r[1] for r in c.execute('PRAGMA table_info(users)')}
        for col,sql in {
            'status_text':"ALTER TABLE users ADD COLUMN status_text TEXT NOT NULL DEFAULT ''",
            'avatar_rev':"ALTER TABLE users ADD COLUMN avatar_rev INTEGER NOT NULL DEFAULT 0",
            'banner_rev':"ALTER TABLE users ADD COLUMN banner_rev INTEGER NOT NULL DEFAULT 0"
        }.items():
            if col not in user_cols: c.execute(sql)
        # Additive migration: old VILO databases are retained.
        cols={r[1] for r in c.execute('PRAGMA table_info(messages)')}
        if 'channel_id' not in cols:
            c.execute('ALTER TABLE messages ADD COLUMN channel_id INTEGER')
        c.executescript('''
          CREATE TABLE IF NOT EXISTS room_config(
             room_id INTEGER PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
             rules TEXT NOT NULL DEFAULT 'Szanuj innych. Nie spamuj. Nie udostępniaj cudzych danych.',
             welcome TEXT NOT NULL DEFAULT 'Witaj na serwerze!',
             accent TEXT NOT NULL DEFAULT '#8b6cf3');
          CREATE TABLE IF NOT EXISTS channels(
             id INTEGER PRIMARY KEY,
             room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
             name TEXT NOT NULL,
             category TEXT NOT NULL DEFAULT 'TEKSTOWE',
             kind TEXT NOT NULL DEFAULT 'text' CHECK(kind IN ('text','announcement','voice')),
             position INTEGER NOT NULL DEFAULT 0,
             staff_only INTEGER NOT NULL DEFAULT 0,
             archived INTEGER NOT NULL DEFAULT 0,
             created INTEGER NOT NULL);
          CREATE INDEX IF NOT EXISTS idx_channels_room ON channels(room_id,position,id);
          CREATE TABLE IF NOT EXISTS custom_roles(
             id INTEGER PRIMARY KEY,
             room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
             name TEXT NOT NULL,
             color TEXT NOT NULL DEFAULT '#9b83f7',
             manage_channels INTEGER NOT NULL DEFAULT 0,
             manage_messages INTEGER NOT NULL DEFAULT 0,
             view_private INTEGER NOT NULL DEFAULT 0,
             created INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS member_custom_roles(
             room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
             user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
             role_id INTEGER NOT NULL REFERENCES custom_roles(id) ON DELETE CASCADE,
             PRIMARY KEY(room_id,user_id,role_id));
        ''')
        if 'archived' not in {r[1] for r in c.execute('PRAGMA table_info(channels)')}:
            c.execute('ALTER TABLE channels ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
        for r in c.execute("SELECT id,kind FROM rooms WHERE kind='server'").fetchall():
            if not c.execute('SELECT 1 FROM channels WHERE room_id=?',(r['id'],)).fetchone():
                cur=c.execute('INSERT INTO channels(room_id,name,category,position,created) VALUES (?,?,?,?,?)',(r['id'],'ogólny','TEKSTOWE',0,int(time.time())))
                c.execute('UPDATE messages SET channel_id=? WHERE room_id=? AND channel_id IS NULL',(cur.lastrowid,r['id']))
                c.execute('INSERT INTO channels(room_id,name,category,kind,position,created) VALUES (?,?,?,?,?,?)',(r['id'],'ogłoszenia','INFORMACJE','announcement',1,int(time.time())))
            c.execute('INSERT OR IGNORE INTO room_config(room_id) VALUES (?)',(r['id'],))
init_db()

class Credentials(BaseModel):
    username:str=Field(min_length=3,max_length=24)
    password:str=Field(min_length=8,max_length=128)
    email:Optional[str]=None
class LoginIn(BaseModel):
    username:str=Field(min_length=3,max_length=254)
    password:str=Field(min_length=1,max_length=128)
class RoomIn(BaseModel):
    name:str=Field(min_length=2,max_length=70)
    kind:str='group'
class DMIn(BaseModel): username:str
class MsgIn(BaseModel):
    body:str=Field(min_length=1,max_length=4000)
    channel_id:Optional[int]=None
class PollIn(BaseModel):
    question:str=Field(min_length=3,max_length=200)
    options:list[str]
    multiple:bool=False
    anonymous:bool=False
    channel_id:Optional[int]=None
class VoteIn(BaseModel): option_ids:list[int]
class BotIn(BaseModel):
    name:str=Field(min_length=2,max_length=32)
    trigger:str=Field(min_length=2,max_length=30)
    response:str=Field(min_length=1,max_length=500)
class ProfileIn(BaseModel):
    display_name:str=Field(min_length=1,max_length=32)
    about:str=Field(max_length=220)
    color:str='#7c5cf0'
    status_text:str=Field(default='',max_length=80)
class ReactionIn(BaseModel): emoji:str
class ChannelIn(BaseModel):
    name:str=Field(min_length=2,max_length=40)
    category:str=Field(default='TEKSTOWE',max_length=32)
    kind:str='text'
    staff_only:bool=False
class RoomSettingsIn(BaseModel):
    name:str=Field(min_length=2,max_length=70)
    description:str=Field(default='',max_length=500)
    rules:str=Field(default='',max_length=6000)
    welcome:str=Field(default='',max_length=300)
    accent:str='#8b6cf3'
class CustomRoleIn(BaseModel):
    name:str=Field(min_length=2,max_length=35)
    color:str='#9b83f7'
    manage_channels:bool=False
    manage_messages:bool=False
    view_private:bool=False
class RoleAssignIn(BaseModel):
    assigned:bool
class InviteIn(BaseModel): code:str
class RoleIn(BaseModel): role:str

USERNAME_RE=re.compile(r'^[a-zA-Z0-9_]{3,24}$')
EMAIL_RE=re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
COLOR_RE=re.compile(r'^#[0-9a-fA-F]{6}$')

def tick(): return int(time.time())
def pw_hash(p):
    salt=secrets.token_bytes(16)
    return 'pbkdf2$'+salt.hex()+'$'+hashlib.pbkdf2_hmac('sha256',p.encode(),salt,310_000).hex()
def pw_check(p,encoded):
    try:
        _,salt,key=encoded.split('$')
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(salt),310_000),bytes.fromhex(key))
    except (ValueError,TypeError): return False

def make_session(resp,uid,req):
    token=secrets.token_urlsafe(40)
    with transaction() as c:
        c.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),uid,tick()+30*86400))
    secure=req.url.scheme=='https' or os.getenv('VILO_SECURE_COOKIES')=='1'
    resp.set_cookie('vilo_session',token,max_age=30*86400,httponly=True,secure=secure,samesite='lax',path='/')

def session_user(token):
    if not token: return None
    return one('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?',hashlib.sha256(token.encode()).hexdigest(),tick())
def current(req:Request):
    u=session_user(req.cookies.get('vilo_session'))
    if not u: raise HTTPException(401,'Zaloguj się, aby kontynuować.')
    return u

def user_public(u):
    return {k:u[k] for k in ('id','username','display_name','about','color','created','status_text','avatar_rev','banner_rev') if k in u.keys()}

def room_member(room_id, uid):
    m=one('SELECT m.*,r.kind,r.name,r.owner_id FROM members m JOIN rooms r ON r.id=m.room_id WHERE m.room_id=? AND m.user_id=?',room_id,uid)
    if not m: raise HTTPException(403,'Nie masz dostępu do tej rozmowy.')
    return m

def room_admin(room_id,uid):
    m=room_member(room_id,uid)
    if m['role'] not in ('owner','admin'): raise HTTPException(403,'Tylko administrator grupy może to zrobić.')
    return m

def server_member(room_id,uid):
    m=room_member(room_id,uid)
    if m['kind']!='server':raise HTTPException(400,'Ta funkcja dotyczy serwerów.')
    return m

def can(room_id, uid, permission):
    m=room_member(room_id,uid)
    if m['role'] in ('owner','admin'):return True
    if permission=='manage_roles':return False
    if permission not in ('manage_channels','manage_messages','view_private'):return False
    row=one(f'''SELECT 1 FROM member_custom_roles mr JOIN custom_roles cr ON cr.id=mr.role_id
                  WHERE mr.room_id=? AND mr.user_id=? AND cr.{permission}=1 LIMIT 1''',room_id,uid)
    return bool(row)

def channel_check(rid, cid, uid, sending=False):
    m=room_member(rid,uid)
    if m['kind']!='server':
        if cid is not None:raise HTTPException(400,'Ta rozmowa nie ma kanałów.')
        return None
    if cid is None:raise HTTPException(400,'Wybierz kanał tekstowy.')
    ch=one('SELECT * FROM channels WHERE id=? AND room_id=?',cid,rid)
    if not ch or ch['archived']:raise HTTPException(404,'Nie ma takiego kanału.')
    if ch['staff_only'] and not can(rid,uid,'view_private'):
        raise HTTPException(403,'Kanał jest prywatny.')
    if sending and ch['kind']=='voice':raise HTTPException(400,'Do kanałów głosowych nie wysyła się wiadomości tekstowych.')
    if sending and ch['kind']=='announcement' and not can(rid,uid,'manage_channels'):
        raise HTTPException(403,'Tylko administracja może publikować ogłoszenia.')
    return ch

def limit(key, n=15, secs=60):
    now=time.monotonic(); prev=[t for t in rate.get(key,[]) if t>now-secs]
    if len(prev)>=n: raise HTTPException(429,'Zbyt wiele prób. Spróbuj później.')
    prev.append(now);rate[key]=prev
    if len(rate)>2000:
        for k in list(rate)[:500]: rate.pop(k,None)

@app.middleware('http')
async def basic_security(req:Request,call_next):
    # Stops cross-site form/API requests in browsers. Local HTTP is for trusted networks only.
    if req.url.path.startswith('/api/') and req.method not in ('GET','HEAD','OPTIONS'):
        origin=req.headers.get('origin','')
        if origin:
            from urllib.parse import urlparse
            o=urlparse(origin)
            if o.netloc != req.headers.get('host','') or o.scheme != req.url.scheme:
                return JSONResponse({'detail':'Nieprawidłowe pochodzenie żądania.'},status_code=403)
    res=await call_next(req)
    res.headers['X-Content-Type-Options']='nosniff'
    res.headers['X-Frame-Options']='DENY'
    res.headers['Referrer-Policy']='same-origin'
    res.headers['Cache-Control']='no-store' if req.url.path.startswith('/api/') else 'no-cache'
    res.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' ws: wss:; font-src 'self'; object-src 'none'; frame-ancestors 'none'"
    return res

@app.post('/api/register')
def register(inp:Credentials,req:Request,response:Response):
    limit('reg:'+str(req.client.host),5,3600)
    if not USERNAME_RE.fullmatch(inp.username): raise HTTPException(400,'Nick: 3–24 znaki, litery, cyfry lub _.')
    if not inp.email or len(inp.email)>254 or not EMAIL_RE.fullmatch(inp.email): raise HTTPException(400,'Podaj prawidłowy adres e-mail.')
    if inp.password.lower() in ('password123','12345678','qwerty123'): raise HTTPException(400,'Wybierz silniejsze hasło.')
    with transaction() as c:
        try:
            cur=c.execute('INSERT INTO users(username,email,pw,display_name,created) VALUES (?,?,?,?,?)',(inp.username,inp.email,pw_hash(inp.password),inp.username,tick()))
        except sqlite3.IntegrityError: raise HTTPException(409,'Nick lub e-mail jest już zajęty.')
    make_session(response,cur.lastrowid,req)
    return {'ok':True}

@app.post('/api/login')
def login(inp:LoginIn,req:Request,response:Response):
    limit('login:'+str(req.client.host),12,900)
    u=one('SELECT * FROM users WHERE username=? OR email=?',inp.username,inp.username)
    if not u or not pw_check(inp.password,u['pw']): raise HTTPException(401,'Nieprawidłowy login lub hasło.')
    make_session(response,u['id'],req)
    return {'ok':True}

@app.post('/api/logout')
def logout(req:Request,response:Response,u=Depends(current)):
    token=req.cookies.get('vilo_session')
    with transaction() as c: c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie('vilo_session',path='/');return {'ok':True}

@app.get('/api/me')
def me(u=Depends(current)): return user_public(u)
@app.patch('/api/me')
def patch_me(data:ProfileIn,u=Depends(current)):
    if not COLOR_RE.fullmatch(data.color):raise HTTPException(400,'Nieprawidłowy kolor.')
    with transaction() as c:c.execute('UPDATE users SET display_name=?,about=?,color=?,status_text=? WHERE id=?',(data.display_name.strip(),data.about.strip(),data.color,data.status_text.strip(),u['id']))
    return {'ok':True}

# A tiny, authenticated image service; untrusted files are decoded and rewritten.
@app.post('/api/me/media/{kind}')
async def upload_profile_media(kind:str,file:UploadFile=File(...),u=Depends(current)):
    if kind not in ('avatar','banner'):raise HTTPException(404,'Nieznany rodzaj obrazu.')
    limit('profile-media:'+str(u['id']),12,3600)
    if Path(file.filename or '').suffix.lower() not in ('.jpg','.jpeg','.png','.webp','.gif'):
        raise HTTPException(400,'Wybierz zdjęcie JPG, PNG lub WebP.')
    buf=await file.read(3*1024*1024+1)
    if len(buf)>3*1024*1024:raise HTTPException(413,'Zdjęcie może mieć maksymalnie 3 MB.')
    try:
        with Image.open(io.BytesIO(buf)) as im:
            if im.width*im.height>12_000_000:raise HTTPException(400,'Zdjęcie ma zbyt duże wymiary.')
            im=ImageOps.exif_transpose(im)
            im.seek(0)
            im=im.convert('RGB')
            im.thumbnail((512,512) if kind=='avatar' else (1440,540),Image.Resampling.LANCZOS)
            out=io.BytesIO();im.save(out,'WEBP',quality=86,method=5)
            encoded=out.getvalue()
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError):
        raise HTTPException(400,'Nie udało się odczytać obrazu.')
    rev=int(time.time()*1000)
    path=PROFILE_MEDIA/f'{u["id"]}-{kind}-{rev}.webp'
    path.write_bytes(encoded)
    field='avatar_rev' if kind=='avatar' else 'banner_rev'
    try:
        with transaction() as c:c.execute(f'UPDATE users SET {field}=? WHERE id=?',(rev,u['id']))
    except Exception:
        path.unlink(missing_ok=True);raise
    for old in PROFILE_MEDIA.glob(f'{u["id"]}-{kind}-*.webp'):
        if old!=path:old.unlink(missing_ok=True)
    return {'ok':True,'rev':rev}

@app.get('/api/profile-media/{uid}/{kind}')
def get_profile_media(uid:int,kind:str,u=Depends(current)):
    if kind not in ('avatar','banner'):raise HTTPException(404,'Nie znaleziono zdjęcia.')
    rec=one('SELECT avatar_rev,banner_rev FROM users WHERE id=?',uid)
    if not rec:raise HTTPException(404,'Nie znaleziono użytkownika.')
    rev=rec['avatar_rev' if kind=='avatar' else 'banner_rev']
    if not rev:raise HTTPException(404,'Nie dodano zdjęcia.')
    path=PROFILE_MEDIA/f'{uid}-{kind}-{rev}.webp'
    if not path.is_file():raise HTTPException(404,'Nie znaleziono zdjęcia.')
    return FileResponse(path,media_type='image/webp',headers={'Cache-Control':'private, max-age=3600','X-Content-Type-Options':'nosniff'})

@app.delete('/api/me/media/{kind}')
def delete_profile_media(kind:str,u=Depends(current)):
    if kind not in ('avatar','banner'):raise HTTPException(404,'Nieznany rodzaj obrazu.')
    field='avatar_rev' if kind=='avatar' else 'banner_rev'
    with transaction() as c:c.execute(f'UPDATE users SET {field}=0 WHERE id=?',(u['id'],))
    for old in PROFILE_MEDIA.glob(f'{u["id"]}-{kind}-*.webp'):old.unlink(missing_ok=True)
    return {'ok':True}

@app.get('/api/users')
def users(q:str='',u=Depends(current)):
    if len(q.strip())<2:return []
    return [user_public(x) for x in allrows('SELECT id,username,display_name,about,color,created,status_text,avatar_rev,banner_rev FROM users WHERE username LIKE ? AND id<>? LIMIT 15',('%'+q.strip()+'%',u['id']))]

@app.get('/api/friends')
def friends(u=Depends(current)):
    rows=allrows('''SELECT f.id friendship_id,f.sender,f.receiver,f.status, x.id,x.username,x.display_name,x.about,x.color,x.created,x.avatar_rev,x.status_text
     FROM friendships f JOIN users x ON x.id=(CASE WHEN f.sender=? THEN f.receiver ELSE f.sender END)
     WHERE f.sender=? OR f.receiver=? ORDER BY f.created DESC''',u['id'],u['id'],u['id'])
    for r in rows:r['direction']='incoming' if r['receiver']==u['id'] else 'outgoing'
    return rows

@app.post('/api/friends/request/{other_id}')
async def friend_request(other_id:int,u=Depends(current)):
    if other_id==u['id']:raise HTTPException(400,'Nie możesz zaprosić siebie.')
    if not one('SELECT id FROM users WHERE id=?',other_id):raise HTTPException(404,'Nie znaleziono użytkownika.')
    with transaction() as c:
        existing=c.execute('SELECT * FROM friendships WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?)',(u['id'],other_id,other_id,u['id'])).fetchone()
        if existing:
            if existing['status']=='accepted':raise HTTPException(409,'Jesteście już znajomymi.')
            if existing['receiver']==u['id']:
                c.execute("UPDATE friendships SET status='accepted' WHERE id=?",(existing['id'],))
            else:raise HTTPException(409,'Zaproszenie już wysłane.')
        else:c.execute('INSERT INTO friendships(sender,receiver,created) VALUES (?,?,?)',(u['id'],other_id,tick()))
    await push_users([u['id'],other_id]);return {'ok':True}

@app.post('/api/friends/{friendship_id}/accept')
async def friend_accept(friendship_id:int,u=Depends(current)):
    f=one('SELECT * FROM friendships WHERE id=?',friendship_id)
    if not f or f['receiver']!=u['id']:raise HTTPException(403,'Brak dostępu.')
    with transaction() as c:c.execute("UPDATE friendships SET status='accepted' WHERE id=?",(friendship_id,))
    await push_users([f['sender'],f['receiver']]);return {'ok':True}

@app.get('/api/rooms')
def rooms(u=Depends(current)):
    rows=allrows('''SELECT r.*,m.role,(SELECT COUNT(*) FROM members WHERE room_id=r.id) member_count,
       (SELECT body FROM messages WHERE room_id=r.id ORDER BY id DESC LIMIT 1) last_message,
       (SELECT created FROM messages WHERE room_id=r.id ORDER BY id DESC LIMIT 1) last_at
       FROM members m JOIN rooms r ON m.room_id=r.id WHERE m.user_id=? ORDER BY COALESCE(last_at,r.created) DESC''',u['id'])
    for row in rows:
        if row['kind']=='server':
            row['last_message']='Kanały i społeczność' # prevent leaking private-channel previews
        if row['kind']=='dm':
            other=one('SELECT u.id,u.username,u.display_name,u.color,u.avatar_rev FROM members m JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND u.id<>?',row['id'],u['id'])
            if other: row['name']=other['display_name'];row['other']=dict(other)
    return rows

async def push_users(ids):
    for uid in set(ids):
        for ws in list(connected.get(uid,set())):
            try:await ws.send_json({'type':'refresh','at':tick()})
            except Exception:connected.get(uid,set()).discard(ws)
async def push_room(room_id):
    ids=[r['user_id'] for r in allrows('SELECT user_id FROM members WHERE room_id=?',room_id)]
    await push_users(ids)

@app.post('/api/rooms')
async def room_create(v:RoomIn,u=Depends(current)):
    if v.kind not in ('group','server'):raise HTTPException(400,'Nieprawidłowy rodzaj grupy.')
    with transaction() as c:
        cur=c.execute('INSERT INTO rooms(kind,name,owner_id,created) VALUES (?,?,?,?)',(v.kind,v.name.strip(),u['id'],tick()))
        room_id=cur.lastrowid
        c.execute('INSERT INTO members VALUES (?,?,?,?)',(room_id,u['id'],'owner',tick()))
        if v.kind=='server':
            c.execute('INSERT INTO room_config(room_id) VALUES (?)',(room_id,))
            c.execute('INSERT INTO channels(room_id,name,category,kind,position,created) VALUES (?,?,?,?,?,?)',(room_id,'ogólny','TEKSTOWE','text',0,tick()))
            c.execute('INSERT INTO channels(room_id,name,category,kind,position,created) VALUES (?,?,?,?,?,?)',(room_id,'ogłoszenia','INFORMACJE','announcement',1,tick()))
    await push_users([u['id']]);return {'id':room_id}

@app.get('/api/rooms/{rid}/studio')
def studio(rid:int,u=Depends(current)):
    m=room_member(rid,u['id'])
    config=one('SELECT * FROM room_config WHERE room_id=?',rid)
    room=one('SELECT * FROM rooms WHERE id=?',rid)
    return {'room':dict(room), 'config':dict(config) if config else {},
            'can_manage':m['role'] in ('owner','admin'),
            'can_manage_channels':can(rid,u['id'],'manage_channels')}

@app.patch('/api/rooms/{rid}/studio')
async def studio_update(rid:int,data:RoomSettingsIn,u=Depends(current)):
    m=room_admin(rid,u['id'])
    if m['kind']=='dm':raise HTTPException(400,'Nie można zmieniać ustawień czatu prywatnego.')
    if not COLOR_RE.fullmatch(data.accent):raise HTTPException(400,'Nieprawidłowy kolor.')
    with transaction() as c:
        c.execute('UPDATE rooms SET name=?,description=? WHERE id=?',(data.name.strip(),data.description.strip(),rid))
        c.execute('INSERT OR IGNORE INTO room_config(room_id) VALUES (?)',(rid,))
        c.execute('UPDATE room_config SET rules=?,welcome=?,accent=? WHERE room_id=?',(data.rules.strip(),data.welcome.strip(),data.accent,rid))
    await push_room(rid);return {'ok':True}

@app.get('/api/rooms/{rid}/channels')
def channel_list(rid:int,u=Depends(current)):
    server_member(rid,u['id'])
    rows=allrows('SELECT * FROM channels WHERE room_id=? AND archived=0 ORDER BY category,position,id',rid)
    return [x for x in rows if not x['staff_only'] or can(rid,u['id'],'view_private')]

@app.post('/api/rooms/{rid}/channels')
async def channel_create(rid:int,data:ChannelIn,u=Depends(current)):
    server_member(rid,u['id'])
    if not can(rid,u['id'],'manage_channels'):raise HTTPException(403,'Brak uprawnień do kanałów.')
    if data.kind not in ('text','announcement','voice'):raise HTTPException(400,'Nieprawidłowy typ kanału.')
    name=data.name.strip().lower().replace(' ','-')
    category=data.category.strip().upper() or 'TEKSTOWE'
    with transaction() as c:
        if c.execute('SELECT COUNT(*) FROM channels WHERE room_id=? AND archived=0',(rid,)).fetchone()[0]>=80:
            raise HTTPException(400,'Limit 80 kanałów na serwer.')
        pos=c.execute('SELECT COALESCE(MAX(position),0)+1 FROM channels WHERE room_id=?',(rid,)).fetchone()[0]
        cid=c.execute('INSERT INTO channels(room_id,name,category,kind,position,staff_only,created) VALUES (?,?,?,?,?,?,?)',(rid,name,category,data.kind,pos,int(data.staff_only),tick())).lastrowid
    await push_room(rid);return {'id':cid}

@app.patch('/api/rooms/{rid}/channels/{cid}')
async def channel_update(rid:int,cid:int,data:ChannelIn,u=Depends(current)):
    server_member(rid,u['id'])
    if not can(rid,u['id'],'manage_channels'):raise HTTPException(403,'Brak uprawnień.')
    if data.kind not in ('text','announcement','voice'):raise HTTPException(400,'Nieprawidłowy typ kanału.')
    with transaction() as c:
        if not c.execute('SELECT 1 FROM channels WHERE id=? AND room_id=?',(cid,rid)).fetchone():raise HTTPException(404,'Nie ma takiego kanału.')
        c.execute('UPDATE channels SET name=?,category=?,kind=?,staff_only=? WHERE id=? AND room_id=?',(data.name.strip().lower().replace(' ','-'),data.category.strip().upper(),data.kind,int(data.staff_only),cid,rid))
    await push_room(rid);return {'ok':True}

@app.delete('/api/rooms/{rid}/channels/{cid}')
async def channel_delete(rid:int,cid:int,u=Depends(current)):
    server_member(rid,u['id'])
    if not can(rid,u['id'],'manage_channels'):raise HTTPException(403,'Brak uprawnień.')
    with transaction() as c:
        ch=c.execute('SELECT * FROM channels WHERE id=? AND room_id=?',(cid,rid)).fetchone()
        if not ch:raise HTTPException(404,'Brak kanału.')
        if ch['kind']!='voice':
            others=c.execute("SELECT COUNT(*) FROM channels WHERE room_id=? AND archived=0 AND kind IN ('text','announcement')",(rid,)).fetchone()[0]
            if others<=1:raise HTTPException(400,'Nie możesz usunąć ostatniego kanału tekstowego.')
        # Archive instead of reassigning private messages into public channels.
        c.execute('UPDATE channels SET archived=1 WHERE id=? AND room_id=?',(cid,rid))
    await push_room(rid);return {'ok':True}

@app.get('/api/rooms/{rid}/roles')
def roles_list(rid:int,u=Depends(current)):
    server_member(rid,u['id'])
    rows=allrows('SELECT * FROM custom_roles WHERE room_id=? ORDER BY id',rid)
    mine=[r['role_id'] for r in allrows('SELECT role_id FROM member_custom_roles WHERE room_id=? AND user_id=?',rid,u['id'])]
    return {'roles':rows,'mine':mine}

@app.post('/api/rooms/{rid}/roles')
async def role_create(rid:int,data:CustomRoleIn,u=Depends(current)):
    m=server_member(rid,u['id'])
    if m['role']!='owner':raise HTTPException(403,'Tylko właściciel tworzy role.')
    if not COLOR_RE.fullmatch(data.color):raise HTTPException(400,'Nieprawidłowy kolor.')
    with transaction() as c:
        if c.execute('SELECT COUNT(*) FROM custom_roles WHERE room_id=?',(rid,)).fetchone()[0]>=40:raise HTTPException(400,'Limit 40 ról.')
        role_id=c.execute('INSERT INTO custom_roles(room_id,name,color,manage_channels,manage_messages,view_private,created) VALUES (?,?,?,?,?,?,?)',(rid,data.name.strip(),data.color,int(data.manage_channels),int(data.manage_messages),int(data.view_private),tick())).lastrowid
    await push_room(rid);return {'id':role_id}

@app.post('/api/rooms/{rid}/roles/{role_id}/users/{user_id}')
async def role_assign(rid:int,role_id:int,user_id:int,data:RoleAssignIn,u=Depends(current)):
    m=server_member(rid,u['id'])
    if m['role']!='owner':raise HTTPException(403,'Tylko właściciel przypisuje role.')
    with transaction() as c:
        if not c.execute('SELECT 1 FROM custom_roles WHERE room_id=? AND id=?',(rid,role_id)).fetchone():raise HTTPException(404,'Nie ma roli.')
        if not c.execute('SELECT 1 FROM members WHERE room_id=? AND user_id=?',(rid,user_id)).fetchone():raise HTTPException(404,'Nie ma członka.')
        if data.assigned:c.execute('INSERT OR IGNORE INTO member_custom_roles VALUES (?,?,?)',(rid,user_id,role_id))
        else:c.execute('DELETE FROM member_custom_roles WHERE room_id=? AND user_id=? AND role_id=?',(rid,user_id,role_id))
    await push_room(rid);return {'ok':True}

@app.delete('/api/rooms/{rid}/roles/{role_id}')
async def role_remove(rid:int,role_id:int,u=Depends(current)):
    m=server_member(rid,u['id'])
    if m['role']!='owner':raise HTTPException(403,'Tylko właściciel usuwa role.')
    with transaction() as c:c.execute('DELETE FROM custom_roles WHERE room_id=? AND id=?',(rid,role_id))
    await push_room(rid);return {'ok':True}

@app.post('/api/dm/{other_id}')
async def dm_create(other_id:int,u=Depends(current)):
    if other_id==u['id']:raise HTTPException(400,'Nie możesz pisać sam do siebie.')
    f=one('SELECT * FROM friendships WHERE status=? AND ((sender=? AND receiver=?) OR (sender=? AND receiver=?))','accepted',u['id'],other_id,other_id,u['id'])
    if not f:raise HTTPException(403,'Najpierw musicie zaakceptować zaproszenie do znajomych.')
    match=one('''SELECT r.id FROM rooms r JOIN members a ON a.room_id=r.id AND a.user_id=? JOIN members b ON b.room_id=r.id AND b.user_id=? WHERE r.kind='dm' LIMIT 1''',u['id'],other_id)
    if match:return {'id':match['id']}
    with transaction() as c:
        cur=c.execute("INSERT INTO rooms(kind,name,owner_id,created) VALUES ('dm','Prywatna rozmowa',?,?)",(u['id'],tick()))
        rid=cur.lastrowid
        for uid in (u['id'],other_id):c.execute('INSERT INTO members VALUES (?,?,?,?)',(rid,uid,'member',tick()))
    await push_users([u['id'],other_id]);return {'id':rid}

@app.get('/api/rooms/{rid}/members')
def members(rid:int,u=Depends(current)):
    room_member(rid,u['id'])
    rows=allrows('SELECT u.id,u.username,u.display_name,u.color,u.about,u.avatar_rev,u.status_text,m.role FROM members m JOIN users u ON u.id=m.user_id WHERE m.room_id=? ORDER BY CASE m.role WHEN "owner" THEN 0 WHEN "admin" THEN 1 ELSE 2 END,u.username',rid)
    for row in rows:row['custom_roles']=[x['role_id'] for x in allrows('SELECT role_id FROM member_custom_roles WHERE room_id=? AND user_id=?',rid,row['id'])]
    return rows

@app.post('/api/rooms/{rid}/members/{other_id}/role')
async def set_role(rid:int,other_id:int,data:RoleIn,u=Depends(current)):
    m=room_member(rid,u['id'])
    if m['role']!='owner' or data.role not in ('admin','member'):raise HTTPException(403,'Tylko właściciel może zmieniać role.')
    if other_id==u['id']:raise HTTPException(400,'Nie możesz zmienić własnej roli.')
    with transaction() as c:
        if not c.execute('SELECT 1 FROM members WHERE room_id=? AND user_id=?',(rid,other_id)).fetchone():raise HTTPException(404,'Nie ma takiej osoby w grupie.')
        c.execute('UPDATE members SET role=? WHERE room_id=? AND user_id=?',(data.role,rid,other_id))
    await push_room(rid);return {'ok':True}

@app.post('/api/rooms/{rid}/invites')
def invite_create(rid:int,u=Depends(current)):
    m=room_admin(rid,u['id'])
    if m['kind']=='dm':raise HTTPException(400,'Nie możesz zapraszać do prywatnego czatu.')
    code=secrets.token_urlsafe(10)
    with transaction() as c:c.execute('INSERT INTO invites VALUES (?,?,?,?,?,?)',(code,rid,u['id'],30,0,tick()+7*86400))
    return {'code':code,'expires':tick()+7*86400,'limit':30}

@app.post('/api/invites/join')
async def invite_join(data:InviteIn,u=Depends(current)):
    code=data.code.strip().split('invite=')[-1]
    with transaction() as c:
        inv=c.execute('SELECT * FROM invites WHERE code=?',(code,)).fetchone()
        if not inv or inv['expires']<tick() or inv['used']>=inv['max_uses']:raise HTTPException(404,'Zaproszenie wygasło albo jest nieprawidłowe.')
        existing=c.execute('SELECT 1 FROM members WHERE room_id=? AND user_id=?',(inv['room_id'],u['id'])).fetchone()
        if not existing:
            c.execute('INSERT INTO members VALUES (?,?,?,?)',(inv['room_id'],u['id'],'member',tick()))
            c.execute('UPDATE invites SET used=used+1 WHERE code=?',(code,))
            r=c.execute('SELECT kind FROM rooms WHERE id=?',(inv['room_id'],)).fetchone()
            if r and r['kind']=='server':
                target=c.execute("SELECT id FROM channels WHERE room_id=? AND kind='text' AND staff_only=0 AND archived=0 ORDER BY position,id LIMIT 1",(inv['room_id'],)).fetchone()
                welcome=c.execute('SELECT welcome FROM room_config WHERE room_id=?',(inv['room_id'],)).fetchone()
                if target and welcome and welcome['welcome']:
                    msg=welcome['welcome'].replace('{user}',u['display_name'])[:300]
                    c.execute("INSERT INTO messages(room_id,bot_name,body,kind,channel_id,created) VALUES (?,? ,?,'bot',?,?)",(inv['room_id'],'VILO',msg,target['id'],tick()))
    await push_room(inv['room_id']);return {'id':inv['room_id']}

@app.post('/api/rooms/{rid}/leave')
async def leave_room(rid:int,u=Depends(current)):
    m=room_member(rid,u['id'])
    if m['role']=='owner':raise HTTPException(400,'Właściciel musi najpierw przekazać grupę. Ta opcja będzie dostępna w kolejnej wersji.')
    with transaction() as c:c.execute('DELETE FROM members WHERE room_id=? AND user_id=?',(rid,u['id']))
    await push_room(rid);return {'ok':True}

def poll_data(pid,uid):
    p=one('SELECT * FROM polls WHERE id=?',pid)
    if not p:return None
    pm=one("SELECT channel_id FROM messages WHERE kind='poll' AND ref_id=?",pid)
    channel_check(p['room_id'],pm['channel_id'] if pm else None,uid)
    choices=allrows('SELECT o.id,o.label,COUNT(v.user_id) count FROM poll_options o LEFT JOIN poll_votes v ON v.option_id=o.id WHERE o.poll_id=? GROUP BY o.id ORDER BY o.id',pid)
    selected=[x['option_id'] for x in allrows('SELECT option_id FROM poll_votes WHERE poll_id=? AND user_id=?',pid,uid)]
    return {'id':pid,'question':p['question'],'multiple':bool(p['multiple']),'anonymous':bool(p['anonymous']),'choices':choices,'selected':selected,'total':sum(x['count'] for x in choices)}

def serialize_message(m,uid):
    d=dict(m)
    if m['kind']=='poll':d['poll']=poll_data(m['ref_id'],uid)
    d['reactions']=allrows('SELECT emoji,COUNT(*) count,MAX(CASE WHEN user_id=? THEN 1 ELSE 0 END) mine FROM reactions WHERE message_id=? GROUP BY emoji',uid,m['id'])
    return d

@app.get('/api/rooms/{rid}/messages')
def get_messages(rid:int,before:int=0,channel_id:Optional[int]=None,u=Depends(current)):
    channel_check(rid,channel_id,u['id'])
    if before:
        rows=allrows('''SELECT m.*,u.username,u.display_name,u.color,u.avatar_rev FROM messages m LEFT JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND m.id<? AND (? IS NULL OR m.channel_id=?) ORDER BY m.id DESC LIMIT 60''',rid,before,channel_id,channel_id)
    else:
        rows=allrows('''SELECT m.*,u.username,u.display_name,u.color,u.avatar_rev FROM messages m LEFT JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND (? IS NULL OR m.channel_id=?) ORDER BY m.id DESC LIMIT 60''',rid,channel_id,channel_id)
    return [serialize_message(m,u['id']) for m in reversed(rows) if (m['channel_id']==channel_id if channel_id is not None else True)][:60]

@app.post('/api/rooms/{rid}/messages')
async def post_message(rid:int,data:MsgIn,req:Request,u=Depends(current)):
    channel_check(rid,data.channel_id,u['id'],sending=True);limit('msg:'+str(u['id']),35,30)
    body=data.body.strip()
    if not body:raise HTTPException(400,'Wiadomość jest pusta.')
    with transaction() as c:
        cur=c.execute('INSERT INTO messages(room_id,user_id,body,created,channel_id) VALUES (?,?,?,?,?)',(rid,u['id'],body,tick(),data.channel_id))
        bots=c.execute('SELECT * FROM bots WHERE room_id=? AND enabled=1',(rid,)).fetchall()
        # Safe predefined responses only; never execute arbitrary bot code.
        for bot in bots:
            if body.casefold().strip()==bot['trigger'].casefold().strip():
                c.execute("INSERT INTO messages(room_id,bot_name,body,kind,created,channel_id) VALUES (?,?,?,'bot',?,?)",(rid,bot['name'],bot['response'],tick(),data.channel_id))
    await push_room(rid);return {'id':cur.lastrowid}

@app.patch('/api/messages/{mid}')
async def edit_message(mid:int,data:MsgIn,u=Depends(current)):
    m=one('SELECT * FROM messages WHERE id=?',mid)
    if m:channel_check(m['room_id'],m['channel_id'],u['id'])
    if not m or m['user_id']!=u['id'] or m['kind']!='text' or m['deleted']:raise HTTPException(403,'Nie możesz edytować tej wiadomości.')
    with transaction() as c:c.execute('UPDATE messages SET body=?,edited=? WHERE id=?',(data.body.strip(),tick(),mid))
    await push_room(m['room_id']);return {'ok':True}

@app.delete('/api/messages/{mid}')
async def delete_message(mid:int,u=Depends(current)):
    m=one('SELECT * FROM messages WHERE id=?',mid)
    if not m:raise HTTPException(404,'Brak wiadomości.')
    membership=room_member(m['room_id'],u['id'])
    channel_check(m['room_id'],m['channel_id'],u['id'])
    if m['user_id']!=u['id'] and not can(m['room_id'],u['id'],'manage_messages'):raise HTTPException(403,'Brak uprawnień.')
    with transaction() as c:c.execute("UPDATE messages SET body='',deleted=1 WHERE id=?",(mid,))
    await push_room(m['room_id']);return {'ok':True}

@app.post('/api/messages/{mid}/react')
async def react(mid:int,data:ReactionIn,u=Depends(current)):
    if data.emoji not in ['👍','❤️','😂','🎉','🔥','👀','💜']:raise HTTPException(400,'Nieznana reakcja.')
    m=one('SELECT * FROM messages WHERE id=?',mid)
    if not m:raise HTTPException(404,'Brak wiadomości.')
    channel_check(m['room_id'],m['channel_id'],u['id'])
    with transaction() as c:
        if c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],data.emoji)).fetchone():
            c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],data.emoji))
        else:c.execute('INSERT INTO reactions VALUES (?,?,?)',(mid,u['id'],data.emoji))
    await push_room(m['room_id']);return {'ok':True}

@app.post('/api/rooms/{rid}/polls')
async def create_poll(rid:int,data:PollIn,u=Depends(current)):
    channel_check(rid,data.channel_id,u['id'],sending=True)
    options=[v.strip() for v in data.options if v.strip()]
    if not 2<=len(options)<=8 or any(len(x)>80 for x in options):raise HTTPException(400,'Ankieta musi mieć od 2 do 8 odpowiedzi (maks. 80 znaków).')
    with transaction() as c:
        p=c.execute('INSERT INTO polls(room_id,question,creator,multiple,anonymous,created) VALUES (?,?,?,?,?,?)',(rid,data.question.strip(),u['id'],int(data.multiple),int(data.anonymous),tick())).lastrowid
        for opt in options:c.execute('INSERT INTO poll_options(poll_id,label) VALUES (?,?)',(p,opt))
        c.execute("INSERT INTO messages(room_id,user_id,body,kind,ref_id,created,channel_id) VALUES (?,?,?,'poll',?,?,?)",(rid,u['id'],data.question.strip(),p,tick(),data.channel_id))
    await push_room(rid);return {'id':p}

@app.post('/api/polls/{pid}/vote')
async def vote(pid:int,data:VoteIn,u=Depends(current)):
    p=one('SELECT * FROM polls WHERE id=?',pid)
    if not p:raise HTTPException(404,'Brak ankiety.')
    pm=one("SELECT channel_id FROM messages WHERE kind='poll' AND ref_id=?",pid)
    channel_check(p['room_id'],pm['channel_id'] if pm else None,u['id'])
    requested=list(dict.fromkeys(data.option_ids))
    allowed={x['id'] for x in allrows('SELECT id FROM poll_options WHERE poll_id=?',pid)}
    if not requested or any(x not in allowed for x in requested) or (not p['multiple'] and len(requested)!=1):raise HTTPException(400,'Nieprawidłowy głos.')
    with transaction() as c:
        c.execute('DELETE FROM poll_votes WHERE poll_id=? AND user_id=?',(pid,u['id']))
        for oid in requested:c.execute('INSERT INTO poll_votes VALUES (?,?,?)',(pid,oid,u['id']))
    await push_room(p['room_id']);return {'ok':True}

@app.get('/api/rooms/{rid}/bots')
def bots(rid:int,u=Depends(current)):
    room_member(rid,u['id'])
    return allrows('SELECT id,name,trigger,response,enabled,owner_id FROM bots WHERE room_id=? ORDER BY id DESC',rid)

@app.post('/api/rooms/{rid}/bots')
async def bot_create(rid:int,data:BotIn,u=Depends(current)):
    room_admin(rid,u['id'])
    if not data.trigger.startswith('/'):raise HTTPException(400,'Komenda bota musi zaczynać się od /, np. /pomoc.')
    with transaction() as c:
        count=c.execute('SELECT COUNT(*) FROM bots WHERE room_id=?',(rid,)).fetchone()[0]
        if count>=12:raise HTTPException(400,'Limit 12 prostych botów na grupę w tej wersji.')
        c.execute('INSERT INTO bots(room_id,owner_id,name,trigger,response,created) VALUES (?,?,?,?,?,?)',(rid,u['id'],data.name.strip(),data.trigger.strip(),data.response.strip(),tick()))
    await push_room(rid);return {'ok':True}

@app.delete('/api/rooms/{rid}/bots/{bid}')
async def bot_remove(rid:int,bid:int,u=Depends(current)):
    room_admin(rid,u['id'])
    with transaction() as c:c.execute('DELETE FROM bots WHERE id=? AND room_id=?',(bid,rid))
    await push_room(rid);return {'ok':True}

ALLOWED={'.png','.jpg','.jpeg','.webp','.gif','.pdf','.txt','.zip','.schem','.json','.md','.csv','.docx','.xlsx','.pptx','.mp4'}
MAX_UPLOAD=10*1024*1024
@app.post('/api/rooms/{rid}/upload')
async def upload(rid:int,req:Request,channel_id:Optional[int]=None,file:UploadFile=File(...),u=Depends(current)):
    channel_check(rid,channel_id,u['id'],sending=True);limit('up:'+str(u['id']),12,60)
    name=Path((file.filename or 'plik').replace('\\','/')).name[:100]
    ext=Path(name).suffix.lower()
    if ext not in ALLOWED:raise HTTPException(400,'Ten rodzaj pliku jest niedozwolony w wersji testowej.')
    contents=await file.read(MAX_UPLOAD+1)
    if len(contents)>MAX_UPLOAD:raise HTTPException(413,'Maksymalny rozmiar pliku to 10 MB.')
    ident=uuid.uuid4().hex;stored=ident+ext
    (UPLOADS/stored).write_bytes(contents)
    try:
        with transaction() as c:
            c.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?,?)',(ident,rid,u['id'],name,stored,len(contents),tick()))
            c.execute("INSERT INTO messages(room_id,user_id,body,kind,attachment_id,attachment_name,created,channel_id) VALUES (?,?,?,'file',?,?,?,?)",(rid,u['id'],name,ident,name,tick(),channel_id))
    except Exception:
        (UPLOADS/stored).unlink(missing_ok=True);raise
    await push_room(rid)
    return {'ok':True}

@app.get('/api/uploads/{ident}')
def download(ident:str,u=Depends(current)):
    record=one('SELECT * FROM uploads WHERE id=?',ident)
    if not record:raise HTTPException(404,'Nie znaleziono pliku.')
    am=one("SELECT channel_id FROM messages WHERE attachment_id=?",ident)
    channel_check(record['room_id'],am['channel_id'] if am else None,u['id'])
    path=UPLOADS/record['stored']
    if not path.is_file():raise HTTPException(404,'Plik nie jest dostępny.')
    return FileResponse(path,filename=record['filename'],media_type='application/octet-stream',content_disposition_type='attachment')

@app.websocket('/ws')
async def ws(websocket:WebSocket):
    origin=websocket.headers.get('origin','')
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc!=websocket.headers.get('host',''):
            await websocket.close(code=1008);return
    u=session_user(websocket.cookies.get('vilo_session'))
    if not u:await websocket.close(code=1008);return
    await websocket.accept()
    connected.setdefault(u['id'],set()).add(websocket)
    await push_users([u['id']])
    try:
        while True:
            msg=await websocket.receive_text()
            if msg=='ping':await websocket.send_json({'type':'pong'})
    except (WebSocketDisconnect,RuntimeError): pass
    finally:
        connected.get(u['id'],set()).discard(websocket)
        await push_users([u['id']])

@app.get('/api/online')
def online(u=Depends(current)):
    return {'ids':[uid for uid,ws in connected.items() if ws]}

@app.get('/api/health')
def health():return {'status':'ok','app':'VILO'}

@app.get('/')
def index():return FileResponse(ROOT/'static'/'index.html')
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

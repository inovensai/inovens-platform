import base64, hashlib, json, os, secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import psycopg
from psycopg.rows import dict_row

ROOT = Path(os.environ.get('PLATFORM_ROOT', '/home/ege/.local/share/inovens-platform'))
TZ = ZoneInfo('Europe/Istanbul')
CONSENT = '2026-09-06-v2'
ROLES = {'member':1_000_000,'board':3_000_000,'president':5_000_000,'vice_president':5_000_000,'owner':10_000_000}
MODELS = ['inovens-combo-fast','inovens-combo-smart','inovens-combo-code']
BOARD = {'owner','president','vice_president','board'}

class Denied(Exception):
    def __init__(self, code, message, status=403):
        self.code,self.message,self.status=code,message,status
        super().__init__(message)

def connect():
    return psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row)

def now(): return datetime.now(timezone.utc)
def day_key(at=None): return (at or now()).astimezone(TZ).date()
def next_reset(at=None):
    local=(at or now()).astimezone(TZ)
    return datetime.combine(local.date()+timedelta(days=1),datetime.min.time(),TZ)
def month_key(at=None): return day_key(at).replace(day=1)
def serialize(v):
    if isinstance(v,(datetime,)):return v.isoformat()
    if isinstance(v,Decimal):return float(v)
    return str(v)
def dumps(v):return json.dumps(v,ensure_ascii=False,default=serialize,separators=(',',':'))
def encrypt(v,key=None):
    secret=hashlib.sha256((key or os.environ['CONTENT_KEY']).encode()).digest();nonce=secrets.token_bytes(12)
    return base64.b64encode(nonce+AESGCM(secret).encrypt(nonce,dumps(v).encode(),None)).decode()
def decrypt(v,key=None):
    secret=hashlib.sha256((key or os.environ['CONTENT_KEY']).encode()).digest();raw=base64.b64decode(v,validate=True)
    return json.loads(AESGCM(secret).decrypt(raw[:12],raw[12:],None))
def user(c,uid,active=True,consent=False):
    u=c.execute('SELECT * FROM platform_users WHERE id=%s',(uid,)).fetchone()
    if not u:raise Denied('account_missing','Hesabınız sunucuya aktarılıyor.',409)
    if active and u['status']!='active':raise Denied('account_inactive','Hesabınız aktif değil.')
    if consent and u['consent_version']!=CONSENT:raise Denied('consent_required','Devam etmek için https://inovensai.com/panel üzerinden güncel kullanım açıklamasını okuyup kabul edin.')
    return u
def owner(u):
    if u['role']!='owner':raise Denied('owner_required','Bu işlem yalnız proje sahibine açıktır.')
def board(u):
    if u['role'] not in BOARD:raise Denied('board_required','Bu kaynak yalnız yetkili yönetim ekibine açıktır.')
def limits(c):return c.execute("SELECT value FROM settings WHERE key='limits'").fetchone()['value']
def daily_limit(u):return u['daily_limit'] if u['daily_limit'] is not None else ROLES[u['role']]
def audit(c,uid,action,target=None,detail=None):
    c.execute('INSERT INTO audit_events(user_id,action,target,detail) VALUES(%s,%s,%s,%s)',(uid,action,str(target) if target is not None else None,dumps(detail or {})))

"""Local, conservative egress gate. No source text is sent to a classifier service.
Public eligibility is intentionally narrow; unrecognized content fails closed.
"""
import re,unicodedata,hashlib,os
from common import *
VERSION='2026-09-20-restricted-route-v4'
PRIVATE_MODEL='inovens-combo-ozel'
PRIVATE_MODELS=['ag/gemini-3.8-flash-low']
PRIVATE_FALLBACK_MODEL='inovens-combo-ozel-yedek'
PRIVATE_FALLBACK_MODELS=['ag/gemini-3.8-flash-medium']
PATTERNS={
 'credential':r'-----BEGIN [^-]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)|\b\d{7,12}:AA[A-Za-z0-9_-]{20,}|(?:authorization\s*[:=]\s*(?:bearer|basic)\s+\S+)|(?:password|passwd|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|sifre|parola)\s*(?::|=|\bis\b)\s*\S+',
 'identity':r'\b[1-9]\d{10}\b|\b(?:passport|pasaport|kimlik|tckn|tc no)\b',
 'finance':r'\bTR\s*\d{2}(?:\s*\d){22}\b|\b(?:\d[ -]?){13,19}\b|\b(?:iban|banka|hesap numarasi|kredi kart|bakiye|maas\w*|borc|odeme|finans|fatura|butce|ucret|tl|usd|eur)\b|[₺$€]',
 'health':r'\b(?:saglik|hastalik|hasta|teshis|tedavi|recete|ilac|kanser|hamile|psikiyatri|depresyon|tahlil|tibbi|fenalasti)\w*',
 'contact':r'[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:\+?90[\s()-]*)?(?:0?5\d{2})[\s()-]*\d{3}[\s()-]*\d{2}[\s()-]*\d{2}|\b(?:telefon|cep no|ev adres|ikamet|dogum tarihi)\b',
 'private':r'\b(?:ozel yazisma|bana ozel|gizli|confidential|private|secret|aramizda|kimseye soyleme|kisisel (?:bilgi|veri|yazisma)|sevgilim|ailem|esim|sifrem|parolam|adresim|numaram)\b',
 'sensitive_request':r'\b(?:sifre(?:ler|leri)?|parola(?:lar|lari)?|api anahtar(?:i|lari)?|erisim anahtar(?:i|lari)?|access token|refresh token|ozel belge|gizli belge|kullanici bilgiler(?:i|ini)|kimlik bilgileri|pasaport bilgileri|banka bilgileri)\b',
 'encoded':r'data:[^;]+;base64,|\b[A-Za-z0-9+/=_-]{70,}\b',
}
def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD',text.casefold().replace('ı','i')) if not unicodedata.combining(c) and unicodedata.category(c) != 'Cf')
def classify(text):
    if not isinstance(text,str):return {'label':'uncertain','reasons':['non_text']}
    if not text.strip():return {'label':'public','reasons':['empty']}
    n=normalize(text)
    hits=[k for k,p in PATTERNS.items() if re.search(p,n,re.I)]
    if hits:return {'label':'sensitive','reasons':hits}
    if len(text)>60000:return {'label':'uncertain','reasons':['oversize']}
    # General conversational requests, without attachments or embedded data.
    if re.fullmatch(r'(selam|merhaba|selam nasilsin|merhaba nasilsin|tesekkur ederim|sagol|hey|hi|hello|nasilsin|gunaydin|iyi aksamlar|iyi geceler|tesekkurler|sag ol|tamam)[\s.!?]*',n):return {'label':'public','reasons':['generic_greeting']}
    if re.fullmatch(r'(?:(?:bana\s+)?(?:kendini|kim oldugunu|ne yapabildigini)\s+(?:tanit|anlat|acikla)|sen kimsin)[\s.!?]*',n):return {'label':'public','reasons':['assistant_identity_request']}
    community_question=r'(?:(?:bana\s+)?inovensi?\s+(?:anlat|tanit|acikla)|(?:bu\s+)?(?:bot|asistan)\s+(?:ne\s+yapabilir|ne\s+yapabilirsin|nedir|kimdir)|(?:topluluk|inovens)\s+(?:icin\s+)?(?:neler?\s+yapabilirsin|nasil\s+yardimci\s+olabilirsin|hakkinda\s+(?:bilgi\s+ver|anlat)|nedir))'
    if re.fullmatch(community_question+r'[\s.!?]*',n):return {'label':'public','reasons':['community_or_assistant_question']}
    if len(n.strip())<=2 and re.fullmatch(r'[a-z.!?\s]+',n):return {'label':'public','reasons':['short_no_data_message']}
    # Short replies that only control an already-described task carry no source data.
    # Sensitive patterns above still take precedence.
    control=r'(?:evet|hayir|onayliyorum|baslat|baslatabilirsin|devam et|devam|durdur|iptal et|sil|kaldir|tekrar dene|yeniden dene|tamam devam et)'
    referenced=r'(?:(?:\d+[.]?\s*(?:ve\s*)?){1,6})(?:numarali\s*)?(?:isleri?|gorevleri?|maddeleri?)\s*(?:sil|kaldir|iptal et|baslat|durdur)'
    if re.fullmatch(r'(?:'+control+'|'+referenced+r')[\s.!?]*',n):return {'label':'public','reasons':['context_control']}
    # Each sentence must have clear public/community context; unknown passages block.
    segments=[s.strip() for s in re.split(r'[\n.!?]+',n) if s.strip()]
    public=r'\b(topluluk|inovens|etkinlik|seminer|atolye|duyuru|tuzuk|yonetmelik|ogrenci|universite|kampus|konferans|gonullu|kulup|herkese acik|public announcement)\b'
    # A community keyword alone cannot establish that a passage is public.
    # Only this small non-personal vocabulary is automatically eligible; other prose
    # needs a verified private route, even when its source is marked community.
    vocabulary=set('topluluk toplulugun toplulugu toplulukta inovens etkinlik etkinligi etkinlikler etkinlikleri seminer semineri seminerler atolye atolyeleri duyuru duyurusu duyurular tuzuk tuzugu yonetmelik ogrenci ogrenciler universite kampus konferans gonullu kulup herkese acik public announcement icin bir iki uc bes fikir oner onerisi onerileri uret yaz hazirla taslak taslagi metin metni kisa uzun basit genel bilgi bilgileri ver olustur listele ozet ozetle anlat nedir nasil ve veya ile bu su tum herkes katilabilir katilim ucretsizdir aciktir duzenliyoruz duzenlenecek yapilacak olacak yapay zeka teknoloji bilim egitim robotik yazilim programlama tanisma tanitim calisma proje projeleri amaci amaclari ogrenme paylasim dayanisma gonulluluk gelistirme desteklemek paylasmak ogrenmek tesekkur tesekkurler guzel yarin bugun haftaya pazartesi sali carsamba persembe cuma cumartesi pazar saat on iki uc dort bes alti yedi sekiz dokuz onbir oniki online cevrimici yuz yuze salon salonda kampuste duzenleniyor'.split())
    words=re.findall(r'[a-z]+|[^\s\w.,!?:;()\-]',n)
    if segments and not re.search(r'\d|[^\x00-\x7f]',n) and set(words)<=vocabulary and all(re.search(public,s) for s in segments):return {'label':'public','reasons':['public_community_context']}
    return {'label':'public','reasons':['general_non_sensitive_text']}

def scan_secrets(value):
    # Inspect values, not serialized object keys. Tool schemas legitimately contain
    # fields named "password" or "api_key"; serializing the whole schema made those
    # field names look like supplied credentials and blocked harmless requests.
    def strings(item):
        if isinstance(item,str):yield item
        elif isinstance(item,dict):
            for child in item.values():yield from strings(child)
        elif isinstance(item,(list,tuple,set)):
            for child in item:yield from strings(child)
    values=list(strings(value));text='\n'.join(values);n=normalize(text)
    if any(re.search(PATTERNS['credential'],normalize(item),re.I) for item in values):return True
    # Actual configured secrets are forbidden even if printed without a label.
    for key,value in os.environ.items():
        if re.search(r'(TOKEN|SECRET|PASSWORD|API_KEY|ROUTER_KEY|CONTENT_KEY|CAPABILITY_KEY|APP_KEY|DATABASE_URL)',key) and len(value)>=8:
            variants=[value,base64.b64encode(value.encode()).decode(),value.encode().hex()]
            if any(normalize(v) in n for v in variants):return True
    return False

def decide(contents,opaque=False):
    checks=[classify(t) for t in contents]
    if opaque:checks.append({'label':'uncertain','reasons':['uninspected_image_or_binary']})
    label='sensitive' if any(x['label']=='sensitive' for x in checks) else 'uncertain' if any(x['label']=='uncertain' for x in checks) else 'public'
    return {'label':label,'reasons':sorted({r for x in checks for r in x['reasons']}),'version':VERSION}

def canonicalize_current_user(messages,current):
    """Discard worker-added wrappers around the host-owned current user message."""
    canonical=[]
    for message in messages:
        item=dict(message)
        if item.get('role')=='user':
            content=item.get('content')
            serialized=content if isinstance(content,str) else dumps(content)
            if current and current in serialized:
                item={'role':'user','content':current}
        canonical.append(item)
    return canonical

def content_text(content):
    if isinstance(content,str):return content,False
    texts=[];opaque=False
    for part in content if isinstance(content,list) else [content]:
        if isinstance(part,dict) and part.get('type')=='text':texts.append(part.get('text',''))
        else:opaque=True
    return '\n'.join(texts),opaque

def enforce(c,uid,jid,result):
    # Sensitive content is isolated from Contributor models. Runtime pinning below
    # prevents a silent fallback from adding a Contributor provider to this route.
    secret='credential' in result.get('reasons',[])
    route='blocked_secret' if secret else 'contributor' if result['label']=='public' else 'private'
    audit(c,uid,'data.route',jid,{'classification':result['label'],'reason_codes':result['reasons'],'route':route,'policy':VERSION})
    if route=='blocked_secret':
        c.execute('UPDATE jobs SET cancel_requested=true,error_code=%s WHERE id=%s',('secret_egress_blocked',jid))
        c.commit()
        raise Denied('secret_egress_blocked','Şifre, erişim tokenı veya sistem sırrı modele gönderilmedi.',409)
    return {**result,'route':route}

def guard_request(c,u,j,b):
    if scan_secrets(b):
        enforce(c,u['id'],j['id'],{'label':'sensitive','reasons':['credential'],'version':VERSION})
    p=decrypt(j['prompt']);contents=[p.get('text','')]
    if p.get('source_policy',{}).get('label') in ['sensitive','uncertain']:
        enforce(c,u['id'],j['id'],p['source_policy'])
    contents += [dumps(d) for d in p.get('documents',[])]
    opaque=bool(p.get('images'))
    # The worker may wrap the current user message in runtime metadata. It is removed
    # locally and therefore never reaches the provider. Older user history and tool
    # results remain untrusted and are classified.
    messages=canonicalize_current_user(b.get('messages',[]),p.get('text',''))
    safe=[]
    for m in messages:
        role=m.get('role');text,has_opaque=content_text(m.get('content'))
        extra={k:v for k,v in m.items() if k not in ['role','content','tool_call_id']}
        if role in ['system','developer']:
            continue # Replaced by the host-owned system message in proxy.py.
        if role not in ['user','tool','function','assistant']:
            opaque=True;continue
        check=classify(text) if text else {'label':'public','reasons':['empty']}
        extra_check=classify(dumps(extra)) if extra else {'label':'public','reasons':['empty']}
        if role in ['tool','function']:
            contents += [text,dumps(extra) if extra else ''];opaque=opaque or has_opaque;safe.append(m);continue
        if check['label']=='sensitive' or extra_check['label']=='sensitive' or has_opaque:
            contents += [text,dumps(extra) if extra else ''];opaque=opaque or has_opaque;safe.append(m);continue
        if role=='assistant':
            # This text was already produced through the same Contributor route. It may
            # supply task context, but cannot override classification of new user data.
            safe.append(m);continue
        if text==p.get('text','') or check['label']=='public':safe.append(m)
        # Old, unclassified user history is omitted instead of being resent.
    b['messages']=safe
    result=decide(contents,opaque)
    # The user explicitly selected a shared YK workspace. Ordinary text that has no
    # sensitive marker may use the general route there; attachments and tool output
    # remain subject to full-content checks. Personal workspaces continue to fail
    # closed when public status cannot be established.
    if result['label']=='uncertain' and u['role'] in BOARD and not opaque and not p.get('documents') and not p.get('images'):
        scope=c.execute('SELECT scope FROM conversations WHERE id=%s AND user_id=%s',(j['conversation_id'],u['id'])).fetchone()
        if scope and scope['scope'] in ['board','google']:
            result={'label':'public','reasons':['authorized_board_general_text'],'version':VERSION}
    # A board member may name recipients while explicitly asking the community
    # Gmail account to send mail. Contact data alone is allowed for this workflow;
    # all other sensitive categories still block, and gmail_send still creates an
    # approval preview instead of sending immediately.
    normalized_prompt=normalize(p.get('text',''))
    mail_intent=bool(re.search(r'\b(?:mail|e-?posta)\w*\b',normalized_prompt) and re.search(r'\b(?:at|gonder|yolla|ilet)\w*\b',normalized_prompt))
    mail_allowed_reasons={'contact','empty','general_non_sensitive_text','uninspected_image_or_binary'}
    if result['label']=='sensitive' and set(result['reasons'])<=mail_allowed_reasons and 'contact' in result['reasons'] and u['role'] in BOARD and not p.get('documents') and not p.get('images'):
        scope=c.execute('SELECT scope FROM conversations WHERE id=%s AND user_id=%s',(j['conversation_id'],u['id'])).fetchone()
        if scope and scope['scope'] in ['board','google']:
            result={'label':'public','reasons':['authorized_board_contact_context' if not mail_intent else 'authorized_gmail_recipient'],'version':VERSION}
    return enforce(c,u['id'],j['id'],result)

def guard_tool(u,j,value):
    result=decide([dumps(value)])
    if scan_secrets(value):result={'label':'sensitive','reasons':['credential'],'version':VERSION}
    with connect() as c:enforce(c,u['id'],j['id'],result)
    return value

def guard_generated_context(u,j,value):
    """Generated YK summaries were already produced by an approved route."""
    result=decide([dumps(value)])
    if scan_secrets(value) or result['label']=='sensitive':
        result={'label':'sensitive','reasons':result['reasons'] or ['credential'],'version':VERSION}
    else:result={'label':'public','reasons':['authorized_generated_board_context'],'version':VERSION}
    with connect() as c:enforce(c,u['id'],j['id'],result)
    return value


def verify_contributor_route(model):
    """Pin the approved general-data route order."""
    import sqlite3,json
    try:
        path=ROOT/'routers/general/db/data.sqlite'
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=2) as c:
            row=c.execute('SELECT models FROM combos WHERE name=?',(model,)).fetchone()
        valid=row and json.loads(row[0])==['ag/gemini-3.8-flash-medium','meta/muse-spark-1.3-contributor']
    except Exception:valid=False
    if not valid:raise Denied('route_verification_required','Model rotası değişmiş veya doğrulanamıyor. Yönetici kontrolü gerekli; veri gönderilmedi.',409)

def verify_private_route(model=PRIVATE_MODEL):
    """Pin the restricted, non-Contributor route order."""
    import sqlite3,json
    try:
        path=ROOT/'routers/general/db/data.sqlite'
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=2) as c:
            row=c.execute('SELECT models FROM combos WHERE name=?',(model,)).fetchone()
        expected=PRIVATE_MODELS if model==PRIVATE_MODEL else PRIVATE_FALLBACK_MODELS if model==PRIVATE_FALLBACK_MODEL else None
        valid=expected is not None and row and json.loads(row[0])==expected
    except Exception:valid=False
    if not valid:raise Denied('private_route_verification_required','Kısıtlı model rotası değişmiş veya doğrulanamıyor. Veri gönderilmedi.',409)

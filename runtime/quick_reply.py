"""Exact conversational greetings use the same metered proxy without agent startup.
Anything requesting work, mentioning documents, or needing tools stays on OpenClaw.
"""
import re
import httpx
from common import *
from proxy import capability

def eligible(j):
    p=decrypt(j['prompt'])
    if p.get('documents') or p.get('images'):return False
    contract=p.get('execution_contract') or {}
    if contract.get('required_tools') or contract.get('required_extensions') or contract.get('source_required') or contract.get('image_requested'):return False
    text=p.get('text','').casefold().strip()
    simple=re.sub(r'[.!?\s]+$','',text)
    if simple in {'selam','merhaba','hey','hi','hello','günaydın','iyi akşamlar','iyi geceler','teşekkürler','teşekkür ederim','sağ ol','sağol','nasılsın','selam nasılsın','merhaba nasılsın'}:return True
    from data_policy import classify
    from retrieval_policy import allows_archive
    classification=classify(p.get('text',''))
    if classification['label']!='public':
        context=None
        if j.get('conversation_id') and j.get('user_id'):
            with connect() as c:
                context=c.execute('SELECT cv.scope,u.role FROM conversations cv JOIN platform_users u ON u.id=cv.user_id WHERE cv.id=%s AND cv.user_id=%s',(j['conversation_id'],j['user_id'])).fetchone()
        relaxed=classification['label']=='uncertain' and context and context['role'] in BOARD and context['scope'] in ['board','google']
        if not relaxed:return False
    if allows_archive(j):return False
    # Legacy jobs may not contain an execution contract; keep the text guard as a fallback.
    return not re.search(r'\b(?:araştır\w*|arastir\w*|web\w*|site\w*|internet\w*|google\w*|drive\w*|gmail\w*|takvim\w*|mail\w*|e-?posta\w*|indir\w*|yükle\w*|yukle\w*|tarayıcı\w*|tarayici\w*|dosya\w*|belge\w*|arşiv\w*|arsiv\w*|hafıza\w*|hafiza\w*|tüz\w*|tuz\w*)',text)

def run(j):
    token=capability(j['user_id'],j['id'])
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(ROOT/'socket/platform.sock')),timeout=45) as client:
        text=decrypt(j['prompt'])['text']
        greeting=bool(re.fullmatch(r'(selam|merhaba|hey|hi|hello|günaydın|iyi akşamlar|iyi geceler|teşekkürler|teşekkür ederim|sağ ol|sağol|nasılsın|selam nasılsın|merhaba nasılsın)[.!?\s]*',text.casefold().strip()))
        instruction='Kısa bir selamlaşmaya Türkçe, doğal ve en fazla iki cümleyle yanıt ver.' if greeting else 'Genel ve hassas olmayan topluluk isteğine Türkçe, doğrudan ve yararlı yanıt ver. Araç kullandığını veya arşive baktığını iddia etme.'
        messages=[{'role':'system','content':'Sen INOVENS yardımcısısın. '+instruction+' Herhangi bir işi yaptığını ya da kullanıcı hakkında özel bilgi bildiğini iddia etme.'}]
        if not greeting:messages.extend(conversation_context(j,text))
        messages.append({'role':'user','content':text})
        r=client.post('http://platform/v1/chat/completions',headers={'Authorization':'Bearer '+token},json={'model':j['model'],'stream':False,'max_tokens':1024 if greeting else 2048,'messages':messages})
    if r.status_code>=400:
        try:error=r.json()['error'];code=error.get('code','provider_error');message=error.get('message','Yanıt alınamadı.')
        except Exception:code='provider_error';message='Yanıt alınamadı.'
        raise Denied(code,message,r.status_code)
    text=r.json()['choices'][0]['message'].get('content')
    if not isinstance(text,str) or not text.strip():raise Denied('empty_worker_response','Model boş yanıt verdi.',502)
    return text.strip()

def conversation_context(j,current_text):
    """Include only public nearby turns so fast follow-ups keep context without leaking private data."""
    if not j.get('conversation_id') or not j.get('user_id'):return []
    from data_policy import classify
    with connect() as c:
        rows=c.execute("SELECT role,body FROM messages WHERE conversation_id=%s AND EXISTS(SELECT 1 FROM conversations WHERE id=%s AND user_id=%s) ORDER BY id DESC LIMIT 12",(j['conversation_id'],j['conversation_id'],j['user_id'])).fetchall()
    result=[]
    for row in reversed(rows):
        body=decrypt(row['body']);text=body.get('text','') if isinstance(body,dict) else str(body)
        if row['role']=='user' and text.strip()==str(current_text).strip() and row is rows[-1]:continue
        if text and classify(text)['label']=='public':result.append({'role':row['role'],'content':text[:3000]})
    if result and result[-1]['role']=='user' and result[-1]['content'].strip()==str(current_text).strip():result.pop()
    return result[-8:]

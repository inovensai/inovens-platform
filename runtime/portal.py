import base64, hashlib, os, secrets, uuid, re
from datetime import timedelta
from common import *
from quota import usage_view, budget_snapshot
from execution_contract import infer
import archive

def public_user(u):
    return {k:u.get(k) for k in ['id','email','name','role','status','telegram_id','daily_limit','google_access','consent_version','consent_at','created_at']}

def sync_profiles(profiles):
    with connect() as c:
        for p in profiles:
            email=p['email'].lower();is_owner=email==os.environ['OWNER_EMAIL'].lower()
            inv=c.execute('SELECT * FROM invitations WHERE email=%s',(email,)).fetchone()
            role='owner' if is_owner else inv['role'] if inv else 'member'
            status='active' if is_owner or inv else 'pending'
            c.execute('INSERT INTO platform_users(id,email,name,role,status,telegram_id) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET email=excluded.email,name=excluded.name',(p['id'],email,p['name'],role,status,p.get('telegram_user_id') if is_owner else None))
            if inv:
                c.execute("UPDATE platform_users SET role=%s,status='active' WHERE id=%s AND status='pending' AND role<>'owner'",(role,p['id']))
                if inv.get('email_status') in ['sent','uncertain']:c.execute('DELETE FROM invitations WHERE email=%s',(email,))

def conversation(c,uid,cid):
    row=c.execute('SELECT * FROM conversations WHERE id=%s AND user_id=%s',(cid,uid)).fetchone()
    if not row:raise Denied('conversation_missing','Konuşma bulunamadı.',404)
    return row

def message_content(value):
    value=decrypt(value)
    if isinstance(value,dict):return {'text':str(value.get('text','')),'attachments':value.get('attachments',[]) if isinstance(value.get('attachments',[]),list) else []}
    return {'text':str(value),'attachments':[]}

def can_document(c,u,d):
    if d['google_source'] and u['role'] not in BOARD:return False
    if d['owner_id']==u['id'] or d['scope']=='community':return True
    if d.get('review_status')=='submitted' and u['role'] in BOARD:return True
    if d['scope']=='owner':return False
    if d['scope']=='board' and u['role'] in BOARD:return True
    return bool(d['scope']=='personal' and c.execute("SELECT 1 FROM document_shares WHERE document_id=%s AND to_user=%s AND status='accepted'",(d['id'],u['id'])).fetchone())

def operation_assignee(c,value,scope):
    if value in (None,''):return None
    try:uid=int(value)
    except (ValueError,TypeError):raise Denied('invalid_assignee','Geçerli bir kullanıcı seçin.',422)
    target=c.execute("SELECT id,role,status FROM platform_users WHERE id=%s",(uid,)).fetchone()
    if not target or target['status']!='active':raise Denied('invalid_assignee','Sorumlu kullanıcı aktif değil.',422)
    if scope=='owner' and target['role']!='owner':raise Denied('invalid_assignee','Sahibe özel iş yalnız proje sahibine atanabilir.',422)
    if scope=='board' and target['role'] not in BOARD:raise Denied('invalid_assignee','YK işi yalnız yetkili yönetim kullanıcısına atanabilir.',422)
    return uid

def add_document(c,u,name,text,scope='personal',google=False,filedata=None,mime='text/plain',generated=False,metadata=None,kind=None,hint=''):
    if scope not in ['personal','community','board','owner']:raise Denied('invalid_scope','Belge görünürlüğü geçersiz.',422)
    if scope!='personal':board(u)
    if scope=='community':board(u)
    if scope=='owner' and u['role']!='owner':raise Denied('owner_scope_required','Proje sahibine özel belge alanı yalnız proje sahibine açıktır.')
    if not name or len(name)>180 or len(text)>1_000_000:raise Denied('invalid_document','Belge boyutu veya adı geçersiz.',422)
    text=text.replace('\x00','')
    did=str(uuid.uuid4());encoded=encrypt({'text':text,'file':filedata});digest=hashlib.sha256((text+(filedata or '')).encode()).hexdigest()
    archive_kind,archive_meta,review=archive.classify_document(name,text,hint,kind,metadata)
    existing=c.execute('SELECT id FROM documents WHERE owner_id=%s AND content_hash=%s AND scope=%s',(u['id'],digest,scope)).fetchone()
    if existing:
        c.execute("UPDATE documents SET archive_kind=CASE WHEN archive_kind='general' THEN %s ELSE archive_kind END,archive_meta=archive_meta||%s::jsonb,review_status=CASE WHEN review_status='ready' THEN %s ELSE review_status END WHERE id=%s",(archive_kind,dumps(archive_meta),review,existing['id']))
        return str(existing['id'])
    size=c.execute('SELECT COALESCE(sum(length(body)),0) n FROM documents WHERE owner_id=%s',(u['id'],)).fetchone()['n']
    if size+len(encoded)>700_000_000:raise Denied('storage_limit','500 MB dosya saklama alanınız doldu.',429)
    c.execute('INSERT INTO documents(id,owner_id,name,scope,google_source,body,content_hash,filename,mime,generated,archive_kind,archive_meta,review_status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(did,u['id'],name,scope,google,encoded,digest,name if filedata else None,mime,generated,archive_kind,dumps(archive_meta),review))
    for start in range(0,len(text),1800):
        chunk=text[start:start+2200]
        c.execute("INSERT INTO document_chunks(document_id,body,search_index) VALUES(%s,%s,to_tsvector('simple',%s))",(did,encrypt(chunk),chunk.casefold()))
    from data_policy import decide
    classification=decide([name,text],opaque=bool(filedata))
    audit(c,u['id'],'data.import_classified',did,{'classification':classification['label'],'reason_codes':classification['reasons'],'policy':classification['version']})
    return did

def search_documents(c,u,query,google_allowed=False):
    # Query only authorized documents before ranking. No inaccessible titles/snippets escape.
    clauses="(d.owner_id=%s OR (d.scope='community' AND NOT d.google_source) OR (d.scope='board' AND %s) OR EXISTS(SELECT 1 FROM document_shares s WHERE s.document_id=d.id AND s.to_user=%s AND s.status='accepted'))"
    rows=c.execute("SELECT d.id,d.name,d.google_source,d.generated,ch.body,ts_rank(ch.search_index,websearch_to_tsquery('simple',%s)) AS rank FROM document_chunks ch JOIN documents d ON d.id=ch.document_id WHERE "+clauses+" AND (NOT d.google_source OR %s) AND ch.search_index@@websearch_to_tsquery('simple',%s) ORDER BY rank DESC LIMIT 5",(query.casefold(),u['id'],u['role'] in BOARD,u['id'],google_allowed,query.casefold())).fetchall()
    return [{'id':str(r['id']),'name':r['name'],'text':decrypt(r['body']),'google_source':r['google_source'],'generated':r['generated']} for r in rows]

def normalize_request_text(text):
    # Common mobile typo: in a document-delivery phrase, "pf" unambiguously means PDF.
    if re.search(r'\bpf\b',text,re.I) and re.search(r'\b(?:şeklinde|seklinde|dosya|rapor|haber|ilet|gönder|gonder)\w*\b',text,re.I):
        return re.sub(r'\bpf\b','PDF',text,flags=re.I)
    return text

def enqueue(c,u,b,channel='web',request_key=None):
    user(c,u['id'],consent=True)
    if not limits(c)['enabled']:raise Denied('maintenance','Yeni iş kabulü geçici olarak kapalı.',503)
    model=b.get('model','inovens-combo-fast')
    if model not in MODELS:raise Denied('invalid_model','Yalnız INOVENS combo seçenekleri kullanılabilir.',422)
    text=b.get('text','')
    if not isinstance(text,str) or not text.strip() or len(text)>40_000:raise Denied('invalid_message','Mesaj boş veya fazla uzun.',422)
    model_text=normalize_request_text(text)
    key=request_key or b.get('request_key') or str(uuid.uuid4())
    if len(key)>200:raise Denied('invalid_request','İstek kimliği geçersiz.',422)
    key=f"{u['id']}:{key}"
    existing=c.execute('SELECT id,conversation_id FROM jobs WHERE request_key=%s',(key,)).fetchone()
    if existing:return existing
    cid=b.get('conversation_id')
    if cid:conv=conversation(c,u['id'],cid)
    else:
        cid=str(uuid.uuid4());scope=b.get('scope','personal')
        if scope not in ['personal','community','board','google']:raise Denied('invalid_scope','Çalışma alanı geçersiz.',422)
        if scope in ['board','google']:board(u)
        c.execute('INSERT INTO conversations(id,user_id,title,scope,channel,model,google_bound) VALUES(%s,%s,%s,%s,%s,%s,%s)',(cid,u['id'],encrypt(text[:70]),scope,channel,model,scope=='google'))
        conv=conversation(c,u['id'],cid)
    files=b.get('document_ids',[])
    if not isinstance(files,list) or len(files)>5:raise Denied('invalid_files','En fazla beş dosya seçin.',422)
    attachment_reference=r'\b(?:bu|bunda|bundaki|burada|attığım|attigim|gönderdiğim|gonderdigim|yüklediğim|yukledigim|dosya|belge|fotoğraf|fotograf|görsel|gorsel|pdf|rapor|tablo|hücre|hucre|sayfa|slayt|ek)\b'
    if not files and cid and re.search(attachment_reference,text.casefold()):
        previous=c.execute("SELECT body FROM messages WHERE conversation_id=%s AND role='user' ORDER BY id DESC LIMIT 20",(cid,)).fetchall()
        for row in previous:
            for item in message_content(row['body'])['attachments']:
                if isinstance(item,dict) and item.get('id') and item['id'] not in files:files.append(item['id'])
                if len(files)>=5:break
            if files:break
    references=[];images=[];source_texts=[];opaque_sources=False;message_attachments=[];archive_candidates=[]
    for did in files:
        d=c.execute('SELECT * FROM documents WHERE id=%s',(did,)).fetchone()
        if not d or not can_document(c,u,d):raise Denied('document_denied','Dosyaya erişiminiz yok.')
        if d['google_source']:
            board(u);c.execute('UPDATE conversations SET google_bound=true WHERE id=%s',(cid,));conv['google_bound']=True
        decoded=decrypt(d['body']);content=decoded['text']
        source_texts += [d['name'],content]
        opaque_sources=opaque_sources or bool(decoded.get('file'))
        if d['mime'].startswith('image/') and decoded.get('file'):
            from PIL import Image
            import io
            image=Image.open(io.BytesIO(base64.b64decode(decoded['file'])));image.thumbnail((1024,1024));buf=io.BytesIO();image.convert('RGB').save(buf,format='JPEG',quality=80);images.append(base64.b64encode(buf.getvalue()).decode())
        elif d['mime']=='application/pdf' and decoded.get('file') and len(content.strip())<80:
            import fitz
            pdf=fitz.open(stream=base64.b64decode(decoded['file']),filetype='pdf')
            for page in list(pdf)[:5]:
                pix=page.get_pixmap(matrix=fitz.Matrix(1.4,1.4),colorspace=fitz.csRGB,alpha=False)
                images.append(base64.b64encode(pix.tobytes('jpeg',jpg_quality=78)).decode())
            pdf.close()
        references.append({'name':d['name'],'text':content[:12000],'source_id':str(d['id'])})
        message_attachments.append({'id':str(d['id']),'name':d['name'],'mime':d['mime']})
        if d['owner_id']==u['id'] and (d.get('review_status')!='ready' or d.get('archive_kind')=='general'):archive_candidates.append(str(d['id']))
    from data_policy import decide
    source_policy=decide(source_texts,opaque_sources)
    jid=str(uuid.uuid4())
    user_message={'text':text,'attachments':message_attachments} if message_attachments else text
    c.execute('INSERT INTO messages(conversation_id,role,body) VALUES(%s,%s,%s)',(cid,'user',encrypt(user_message)))
    c.execute('INSERT INTO jobs(id,user_id,conversation_id,channel,model,prompt,request_key) VALUES(%s,%s,%s,%s,%s,%s,%s)',(jid,u['id'],cid,channel,model,encrypt({'text':model_text,'documents':references,'images':images,'archive_candidates':archive_candidates,'source_policy':source_policy,'execution_contract':infer(model_text,conv['scope'])}),key))
    c.execute('UPDATE conversations SET updated_at=now(),model=%s WHERE id=%s',(model,cid))
    return {'id':jid,'conversation_id':cid}

def google_ready(c):
    # Owner explicitly confirmed provider data-sharing is disabled on 2026-09-06.
    p=c.execute("SELECT value FROM settings WHERE key='google_routing'").fetchone()
    if p and p['value'].get('owner_confirmed_no_training'):return True
    return False

def dispatch(uid,path,method,b):
    # PHP associative decoding round-trips an empty JSON object as [].
    if b == []:b={}
    if not isinstance(b,dict):raise Denied('invalid_body','İstek gövdesi bir nesne olmalıdır.',422)
    with connect() as c:
        u=user(c,uid,active=False)
        if path=='profile':return {'user':public_user(u),'consent_version':CONSENT,'models':MODELS,'quota':usage_view(c,uid),'google_ready':google_ready(c)}
        if path=='consent' and method=='POST':
            if b.get('version')!=CONSENT or b.get('accepted') is not True:raise Denied('consent_required','Kullanım açıklaması için açık kabul gerekli.',422)
            c.execute('UPDATE platform_users SET consent_version=%s,consent_at=now() WHERE id=%s',(CONSENT,uid));audit(c,uid,'consent.accept',CONSENT);return {'ok':True}
        if path=='privacy/export':
            cs=c.execute('SELECT id,title,scope,created_at FROM conversations WHERE user_id=%s',(uid,)).fetchall()
            for conv in cs:
                conv['title']=decrypt(conv['title']);conv['messages']=[{'role':m['role'],**message_content(m['body']),'at':m['created_at']} for m in c.execute('SELECT role,body,created_at FROM messages WHERE conversation_id=%s ORDER BY id',(conv['id'],))]
            docs=[{'id':d['id'],'name':d['name'],'content':decrypt(d['body'])} for d in c.execute('SELECT * FROM documents WHERE owner_id=%s',(uid,))]
            shares=archive.list_shares(c,u)['items']
            for share in shares:
                share['messages']=archive.list_messages(c,u,str(share['id']))['items'] if share['status']=='accepted' else []
            return {'user':public_user(u),'conversations':cs,'documents':docs,'shares':shares}
        u=user(c,uid)
        if path=='client-errors' and method=='POST':
            from incident_center import record
            record('web-ui','client_error','Web panelinde istemci hatası oluştu.',{'message':str(b.get('message',''))[:300],'page':str(b.get('page',''))[:80],'line':int(b.get('line',0) or 0),'user_id':uid},'error',connection=c,fingerprint_key=str(b.get('message',''))[:160]);return {'ok':True}
        if path=='usage':return {'quota':usage_view(c,uid),'history':c.execute('SELECT day,tokens,cost_try FROM daily_usage WHERE user_id=%s AND day>=CURRENT_DATE-7 ORDER BY day',(uid,)).fetchall()}
        if path=='dashboard':
            active=c.execute("SELECT count(*) n FROM jobs WHERE user_id=%s AND status IN ('queued','running')",(uid,)).fetchone()['n']
            result={'quota':usage_view(c,uid),'active_jobs':active,'google_ready':google_ready(c),'enabled':limits(c)['enabled']}
            if u['role'] in BOARD:
                result.update(budget_snapshot(c));result['limits']=limits(c)
                result['active_users']=c.execute("SELECT count(*) n FROM platform_users WHERE status='active'").fetchone()['n']
                result['pending_users']=c.execute("SELECT count(*) n FROM platform_users WHERE status='pending'").fetchone()['n']
                result['running_jobs']=c.execute("SELECT count(*) n FROM jobs WHERE status='running'").fetchone()['n']
                result['queued_jobs']=c.execute("SELECT count(*) n FROM jobs WHERE status='queued'").fetchone()['n']
            return result
        if path=='conversations' and method=='GET':
            rows=c.execute('SELECT id,title,scope,channel,model,google_bound,updated_at FROM conversations WHERE user_id=%s ORDER BY updated_at DESC LIMIT 100',(uid,)).fetchall()
            for r in rows:r['title']=decrypt(r['title'])
            return {'items':rows}
        match=re.fullmatch(r'conversations/([a-f0-9-]{36})',path)
        if match:
            conv=conversation(c,uid,match[1])
            if method=='DELETE':
                c.execute('UPDATE jobs SET cancel_requested=true WHERE user_id=%s',(uid,));c.execute('UPDATE platform_users SET workspace_epoch=workspace_epoch+1 WHERE id=%s',(uid,));c.execute('INSERT INTO deletion_tombstones(user_id,kind,target) VALUES(%s,%s,%s)',(uid,'conversation',str(conv['id'])));c.execute('DELETE FROM conversations WHERE id=%s',(conv['id'],));return {'ok':True}
            rows=c.execute('SELECT id,role,body,created_at FROM messages WHERE conversation_id=%s ORDER BY id',(conv['id'],)).fetchall()
            for r in rows:r.update(message_content(r.pop('body')))
            conv['title']=decrypt(conv['title']);return {'conversation':conv,'items':rows}
        if path=='chat' and method=='POST':return enqueue(c,u,b)
        if path=='jobs':
            admin=b.get('all') is True
            if admin:owner(u)
            sql='SELECT j.id,j.user_id,j.conversation_id,j.channel,j.status,j.model,j.display_title,j.created_at,j.started_at,j.finished_at,j.heartbeat_at,j.error_code,j.call_count,j.cancel_requested,u.name,cv.title conversation_title,COALESCE((SELECT sum(input_tokens+output_tokens) FROM model_calls m WHERE m.job_id=j.id),0) tokens,COALESCE((SELECT sum(cost_try) FROM model_calls m WHERE m.job_id=j.id),0) cost_try FROM jobs j JOIN platform_users u ON u.id=j.user_id LEFT JOIN conversations cv ON cv.id=j.conversation_id'
            where=' WHERE j.hidden_at IS NULL'+('' if admin else ' AND j.user_id=%s')
            rows=c.execute(sql+where+' ORDER BY j.created_at DESC LIMIT 100',() if admin else (uid,)).fetchall()
            for row in rows:
                display=row.pop('display_title',None);conversation_title=row.pop('conversation_title',None)
                row['title']=decrypt(display or conversation_title) if row['user_id']==uid and (display or conversation_title) else None
            return {'items':rows}
        match=re.fullmatch(r'jobs/([a-f0-9-]{36})',path)
        if match and method in ['PATCH','DELETE']:
            j=c.execute('SELECT * FROM jobs WHERE id=%s AND hidden_at IS NULL',(match[1],)).fetchone()
            if not j or (j['user_id']!=uid and u['role'] not in BOARD):raise Denied('job_missing','İş bulunamadı.',404)
            if method=='PATCH':
                if j['user_id']!=uid:raise Denied('job_owner','Yalnız işi başlatan kullanıcı iş adını değiştirebilir.')
                title=str(b.get('title','')).strip()
                if not title or len(title)>120:raise Denied('invalid_job_title','İş adı gerekli, en fazla 120 karakter olabilir.',422)
                c.execute('UPDATE jobs SET display_title=%s WHERE id=%s',(encrypt(title),j['id']))
                audit(c,uid,'job.rename',j['id'])
            else:
                c.execute("UPDATE jobs SET hidden_at=now(),cancel_requested=true,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=%s",(j['id'],))
                audit(c,uid,'job.hide',j['id'],{'owner_action':j['user_id']!=uid})
            return {'ok':True}
        match=re.fullmatch(r'jobs/([a-f0-9-]{36})/(cancel|grant)',path)
        if match and method=='POST':
            j=c.execute('SELECT * FROM jobs WHERE id=%s',(match[1],)).fetchone()
            if not j or (j['user_id']!=uid and u['role'] not in BOARD):raise Denied('job_missing','İş bulunamadı.',404)
            if match[2]=='cancel':c.execute("UPDATE jobs SET cancel_requested=true,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=%s",(j['id'],))
            else:
                owner(u);amount=int(b.get('amount',10_000_000))
                if not 1<=amount<=50_000_000:raise Denied('invalid_limit','Ek kota 1–50 milyon token arasında olmalıdır.',422)
                c.execute('INSERT INTO quota_grants(id,user_id,job_id,amount,expires_at,created_by) VALUES(%s,%s,%s,%s,%s,%s)',(str(uuid.uuid4()),j['user_id'],j['id'],amount,next_reset(),uid))
                c.execute('UPDATE jobs SET research=true WHERE id=%s',(j['id'],))
                audit(c,uid,'quota.grant',j['id'],{'tokens':amount})
            return {'ok':True}
        if path=='users':
            owner(u);rows=c.execute('SELECT * FROM platform_users ORDER BY id').fetchall()
            return {'items':[{**public_user(x),'quota':usage_view(c,x['id'])} for x in rows]}
        match=re.fullmatch(r'users/(\d+)',path)
        if match and method=='PATCH':
            owner(u);target=user(c,int(match[1]),active=False)
            if target['role']=='owner' and any(k in b for k in ['role','status']):raise Denied('owner_protected','Proje sahibinin rolü veya erişimi değiştirilemez.',422)
            role=b.get('role',target['role']);status=b.get('status',target['status']);limit=b.get('daily_limit',target['daily_limit'])
            if role=='owner' and target['role']!='owner':raise Denied('owner_protected','Proje sahibi rolü başka kullanıcıya atanamaz.',422)
            if role not in ROLES or status not in ['pending','active','revoked'] or (limit is not None and (not isinstance(limit,int) or not 0<=limit<=100_000_000)):raise Denied('invalid_user','Rol, durum veya limit geçersiz.',422)
            c.execute('SELECT pg_advisory_xact_lock(761905)')
            count=c.execute("SELECT count(*) n FROM platform_users WHERE status='active' AND id<>%s",(target['id'],)).fetchone()['n']
            if status=='active' and count>=int(limits(c)['pilot_size']):raise Denied('pilot_full','Ücretsiz pilot kullanıcı sınırına ulaştı.',409)
            google=role in BOARD
            c.execute('UPDATE platform_users SET role=%s,status=%s,daily_limit=%s,google_access=%s WHERE id=%s',(role,status,limit,google,target['id']))
            if status!='active' or role!=target['role'] or google!=target['google_access']:
                c.execute('UPDATE platform_users SET workspace_epoch=workspace_epoch+1 WHERE id=%s',(target['id'],))
                c.execute("UPDATE jobs SET cancel_requested=true WHERE user_id=%s AND status IN ('queued','running')",(target['id'],))
            audit(c,uid,'user.update',target['id'],{'role':role,'status':status,'daily_limit':limit,'google_access':google});return {'ok':True}
        if path=='invitations':
            owner(u)
            if method=='POST':
                email=str(b.get('email','')).strip().lower();role=b.get('role','member')
                if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or role not in ROLES or role=='owner':raise Denied('invalid_invitation','E-posta veya rol geçersiz.',422)
                c.execute('SELECT pg_advisory_xact_lock(761905)')
                total=c.execute("SELECT (SELECT count(*) FROM platform_users WHERE status='active')+(SELECT count(*) FROM invitations i WHERE NOT EXISTS(SELECT 1 FROM platform_users u WHERE u.email=i.email AND u.status='active')) n").fetchone()['n']
                if total>=int(limits(c)['pilot_size']):raise Denied('pilot_full','Pilot kullanıcı sınırı doldu.',409)
                c.execute("INSERT INTO invitations(email,role,created_by,email_status,email_sent_at,email_error) VALUES(%s,%s,%s,'pending',NULL,NULL) ON CONFLICT(email) DO UPDATE SET role=excluded.role,created_by=excluded.created_by,email_status='pending',email_sent_at=NULL,email_error=NULL",(email,role,uid));audit(c,uid,'invitation.create',email,{'role':role,'email_queued':True})
            if method=='DELETE':c.execute('DELETE FROM invitations WHERE email=%s',(b.get('email'),))
            return {'items':c.execute('SELECT * FROM invitations ORDER BY created_at DESC').fetchall(),'email_queued':method=='POST'}
        if path=='settings':
            owner(u)
            if method=='PATCH':
                cfg=limits(c)
                for key in ['daily_try','monthly_try','usd_try','pilot_size']:
                    if key in b:
                        v=float(b[key])
                        if not 0<v<=100000:raise Denied('invalid_setting','Bütçe veya kur değeri geçersiz.',422)
                        cfg[key]=int(v) if key=='pilot_size' else v
                if 'enabled' in b:cfg['enabled']=b['enabled'] is True
                c.execute("UPDATE settings SET value=%s WHERE key='limits'",(dumps(cfg),));audit(c,uid,'settings.update',detail=cfg)
            return {'limits':limits(c),'google_ready':google_ready(c),'models':MODELS}
        if path=='telegram/link' and method=='POST':
            user(c,uid,consent=True);token=secrets.token_urlsafe(24);digest=hashlib.sha256(token.encode()).hexdigest()
            c.execute('DELETE FROM link_tokens WHERE user_id=%s',(uid,));c.execute('INSERT INTO link_tokens(hash,user_id,expires_at) VALUES(%s,%s,%s)',(digest,uid,now()+timedelta(minutes=5)))
            return {'url':'https://t.me/'+os.environ.get('TELEGRAM_USERNAME','inovens_bot')+'?start='+token,'expires_at':(now()+timedelta(minutes=5)).isoformat()}
        if path=='telegram/pair-code' and method=='POST':
            code=str(b.get('code','')).strip().upper()
            if not re.fullmatch(r'[A-Z2-9]{8}',code):raise Denied('invalid_pair_code','Eşleme kodu sekiz karakter olmalıdır.',422)
            pair=c.execute('SELECT * FROM telegram_pair_codes WHERE code=%s AND used_at IS NULL AND expires_at>now() FOR UPDATE',(code,)).fetchone()
            if not pair:raise Denied('invalid_pair_code','Eşleme kodu geçersiz veya süresi dolmuş.',422)
            existing=c.execute('SELECT id FROM platform_users WHERE telegram_id=%s AND id<>%s',(pair['telegram_id'],uid)).fetchone()
            if existing:raise Denied('already_linked','Bu Telegram hesabı başka bir kullanıcıya bağlı.',409)
            c.execute('UPDATE platform_users SET telegram_id=NULL WHERE id=%s',(uid,))
            c.execute('UPDATE platform_users SET telegram_id=%s WHERE id=%s',(pair['telegram_id'],uid))
            c.execute('UPDATE telegram_pair_codes SET used_at=now() WHERE code=%s',(code,))
            audit(c,uid,'telegram.link_code',str(pair['telegram_id']))
            return {'ok':True,'telegram_id':pair['telegram_id']}
        if path=='telegram/unlink' and method=='POST':c.execute('UPDATE platform_users SET telegram_id=NULL WHERE id=%s',(uid,));c.execute("UPDATE jobs SET cancel_requested=true WHERE user_id=%s AND channel='telegram' AND status IN ('queued','running')",(uid,));return {'ok':True}
        if path=='documents':
            if method=='POST':
                name=str(b.get('name','')).strip();text=str(b.get('text',''));filedata=b.get('file');mime=b.get('mime','text/plain')
                if filedata:
                    raw=base64.b64decode(filedata,validate=True)
                    if len(raw)>25_000_000:raise Denied('file_too_large','Dosya en fazla 25 MB olabilir.',422)
                    from extraction import inspect
                    try:text,mime=inspect(raw,name,mime)
                    except Exception:raise Denied('file_unreadable','Dosya türü desteklenmiyor, bozuk veya içeriği uzantısıyla eşleşmiyor.',422)
                did=add_document(c,u,name,text,b.get('scope','personal'),filedata=filedata,mime=mime,metadata=b.get('metadata'),kind=b.get('kind'),hint=b.get('description',''))
                return {'id':did}
            return archive.list_documents(c,u,b.get('query',''),b,limit=b.get('limit',100),offset=b.get('offset',0))
        if path=='archive':
            if method=='POST':
                title=str(b.get('name','')).strip();content=str(b.get('text','')).strip()
                if not title or not content:raise Denied('note_invalid','Not başlığı ve içeriği gerekli.',422)
                kind=b.get('kind','note')
                did=archive.create_memory(c,u,title,content,kind,b.get('metadata'),b.get('scope','personal')) if kind in {'note','idea','reminder'} else add_document(c,u,title,content,b.get('scope','personal'),kind=kind,metadata=b.get('metadata'))
                return {'id':did}
            return archive.list_documents(c,u,b.get('query',''),b,limit=b.get('limit',50),offset=b.get('offset',0))
        match=re.fullmatch(r'archive/review/([a-f0-9-]{36})',path)
        if match and method=='POST':
            board(u);decision=b.get('decision');d=c.execute("SELECT * FROM documents WHERE id=%s AND review_status='submitted' FOR UPDATE",(match[1],)).fetchone()
            if not d:raise Denied('review_missing','İncelenecek katkı bulunamadı.',404)
            if decision=='approve':c.execute("UPDATE documents SET scope='community',review_status='ready' WHERE id=%s",(d['id'],))
            elif decision=='reject':c.execute("UPDATE documents SET scope='personal',review_status='rejected' WHERE id=%s",(d['id'],))
            else:raise Denied('review_invalid','İnceleme kararı geçersiz.',422)
            audit(c,uid,'archive.review_'+decision,str(d['id']),{'owner_id':d['owner_id']});return {'ok':True}
        if path=='directory':
            return {'items':c.execute("SELECT id,name,role FROM platform_users WHERE status='active' AND id<>%s ORDER BY name LIMIT 200",(uid,)).fetchall()}
        if path=='shares':
            if method=='POST':return archive.create_share(c,u,str(b.get('document_id','')),str(b.get('recipient','')),b.get('message',''))
            return archive.list_shares(c,u)
        match=re.fullmatch(r'shares/([a-f0-9-]{36})',path)
        if match and method=='POST':return archive.share_action(c,u,match[1],str(b.get('action','')))
        match=re.fullmatch(r'shares/([a-f0-9-]{36})/messages',path)
        if match:
            if method=='POST':return archive.send_message(c,u,match[1],b.get('text',''))
            return archive.list_messages(c,u,match[1])
        if path=='bot-memory':
            owner(u)
            if method=='POST':
                title=str(b.get('title','')).strip()[:120];content=str(b.get('text','')).strip()[:2500]
                if not title or not content:raise Denied('memory_invalid','Başlık ve içerik gerekli.',422)
                from data_policy import classify
                if classify(title+'\n'+content)['label']=='sensitive':raise Denied('memory_sensitive','Bot hafızasına hassas veya kişisel veri eklenemez.',422)
                row=c.execute('INSERT INTO bot_memory(title,body,created_by) VALUES(%s,%s,%s) RETURNING id',(title,encrypt(content),uid)).fetchone()
                audit(c,uid,'bot_memory.create',row['id']);return {'id':row['id']}
            rows=c.execute('SELECT id,title,body,active,created_at FROM bot_memory ORDER BY id DESC LIMIT 100').fetchall()
            for row in rows:row['body']=decrypt(row['body'])
            return {'items':rows}
        match=re.fullmatch(r'bot-memory/(\d+)',path)
        if match and method=='PATCH':
            owner(u);active=b.get('active') is True
            c.execute('UPDATE bot_memory SET active=%s WHERE id=%s',(active,int(match[1])));audit(c,uid,'bot_memory.toggle',match[1],{'active':active});return {'ok':True}
        match=re.fullmatch(r'documents/([a-f0-9-]{36})',path)
        if match:
            d=c.execute('SELECT * FROM documents WHERE id=%s',(match[1],)).fetchone()
            if not d or not can_document(c,u,d):raise Denied('document_missing','Belge bulunamadı.',404)
            if method in ['DELETE','PATCH'] and d['owner_id']!=uid:raise Denied('document_owner','Yalnız belge sahibi değiştirebilir.')
            if method=='DELETE':c.execute('DELETE FROM documents WHERE id=%s',(d['id'],));c.execute('INSERT INTO deletion_tombstones(user_id,kind,target) VALUES(%s,%s,%s)',(uid,'document',str(d['id'])));return {'ok':True}
            if method=='PATCH':
                if 'metadata' in b or 'kind' in b or 'review_status' in b:
                    meta={**dict(d['archive_meta'] or {}),**archive.validated_metadata(b.get('metadata'))}
                    kind=b.get('kind',d['archive_kind'])
                    if kind not in archive.KINDS:raise Denied('archive_kind_invalid','Arşiv türü geçersiz.',422)
                    review=b.get('review_status',d['review_status'])
                    if review not in {'ready','needs_review'}:raise Denied('review_status_invalid','Gözden geçirme durumu geçersiz.',422)
                    c.execute('UPDATE documents SET archive_meta=%s,archive_kind=%s,review_status=%s WHERE id=%s',(dumps(meta),kind,review,d['id']))
                    audit(c,uid,'document.metadata_updated',str(d['id']))
                if 'scope' in b:
                    scope=b['scope'];board(u)
                    if scope not in ['personal','community','board','owner']:raise Denied('invalid_scope','Görünürlük geçersiz.',422)
                    if scope=='owner':owner(u)
                    c.execute('UPDATE documents SET scope=%s WHERE id=%s',(scope,d['id']))
                    if scope!='personal':
                        c.execute("UPDATE document_shares SET status='revoked',updated_at=now() WHERE document_id=%s AND status IN ('pending','accepted')",(d['id'],))
                        c.execute('DELETE FROM document_acl WHERE document_id=%s',(d['id'],))
                return {'ok':True}
            return {'name':d['name'],'mime':d['mime'],**decrypt(d['body'])}
        if path=='knowledge/search':return {'items':search_documents(c,u,str(b.get('query',''))[:500])}
        if path=='operations':
            if method=='POST':
                board(u);title=str(b.get('title','')).strip()
                if not title or len(title)>220:raise Denied('invalid_title','Başlık gerekli, en fazla 220 karakter.',422)
                scope=b.get('scope','board')
                if scope=='community':board(u)
                if scope not in ['board','owner','community']:raise Denied('invalid_scope','Görünürlük geçersiz.',422)
                if scope=='owner':owner(u)
                start_date=b.get('start_date') or None;due_date=b.get('due_date') or None;status=b.get('status','open')
                if start_date and due_date and due_date<start_date:raise Denied('invalid_date_range','Bitiş tarihi başlangıç tarihinden önce olamaz.',422)
                if status not in ['candidate','open','running','paused','done','cancelled']:raise Denied('invalid_status','İş durumu geçersiz.',422)
                assignee=operation_assignee(c,b.get('assignee_user_id'),scope)
                c.execute('INSERT INTO operations(kind,title,owner_name,assignee_user_id,start_date,due_date,status,note,scope,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(b.get('kind','task'),title,str(b.get('owner_name',''))[:160],assignee,start_date,due_date,status,str(b.get('note',''))[:5000],scope,uid))
                audit(c,uid,'operation.create',title,{'assignee_user_id':assignee,'scope':scope})
            rows=c.execute("SELECT o.*,a.name assignee_name FROM operations o LEFT JOIN platform_users a ON a.id=o.assignee_user_id WHERE o.scope='community' OR (o.scope='board' AND %s) OR (o.created_by=%s AND o.scope<>'owner') OR %s ORDER BY o.updated_at DESC LIMIT 250",(u['role'] in BOARD,uid,u['role']=='owner')).fetchall()
            assignees=c.execute("SELECT id,name,role FROM platform_users WHERE status='active' ORDER BY name").fetchall() if u['role'] in BOARD else []
            return {'items':rows,'assignees':assignees}
        match=re.fullmatch(r'operations/(\d+)',path)
        if match and method in ['PATCH','DELETE']:
            board(u);item=c.execute('SELECT * FROM operations WHERE id=%s',(int(match[1]),)).fetchone()
            if not item:raise Denied('operation_missing','Topluluk işi bulunamadı.',404)
            if item['scope']=='owner':owner(u)
            if method=='DELETE':
                c.execute('DELETE FROM operations WHERE id=%s',(item['id'],));audit(c,uid,'operation.delete',str(item['id']));return {'ok':True}
            title=str(b.get('title',item['title'])).strip();owner_name=str(b.get('owner_name',item['owner_name'])).strip();note=str(b.get('note',item['note'])).strip()
            start_date=b.get('start_date',item['start_date']) or None;due_date=b.get('due_date',item['due_date']) or None;status=b.get('status',item['status']);scope=b.get('scope',item['scope'])
            if not title or len(title)>220:raise Denied('invalid_title','Başlık gerekli, en fazla 220 karakter.',422)
            if len(owner_name)>160 or len(note)>5000:raise Denied('invalid_operation','Sorumlu veya not alanı fazla uzun.',422)
            if status not in ['candidate','open','running','paused','done','cancelled']:raise Denied('invalid_status','İş durumu geçersiz.',422)
            if scope not in ['board','owner','community']:raise Denied('invalid_scope','Görünürlük geçersiz.',422)
            if scope=='owner':owner(u)
            if start_date and due_date and due_date<start_date:raise Denied('invalid_date_range','Bitiş tarihi başlangıç tarihinden önce olamaz.',422)
            assignee=operation_assignee(c,b.get('assignee_user_id',item['assignee_user_id']),scope)
            c.execute('UPDATE operations SET title=%s,owner_name=%s,assignee_user_id=%s,start_date=%s,due_date=%s,status=%s,note=%s,scope=%s,updated_at=now() WHERE id=%s',(title,owner_name,assignee,start_date,due_date,status,note,scope,item['id']))
            audit(c,uid,'operation.update',str(item['id']),{'status':status,'scope':scope,'assignee_user_id':assignee});return {'ok':True}
        if path=='decisions':
            board(u)
            if method=='POST':
                title=str(b.get('title','')).strip();decision=str(b.get('decision','')).strip();meeting_date=b.get('meeting_date');status=b.get('status','accepted')
                if not title or len(title)>220 or not decision or len(decision)>10000:raise Denied('invalid_decision','Başlık ve karar metni gereklidir.',422)
                if not meeting_date:raise Denied('invalid_decision_date','Toplantı tarihi gereklidir.',422)
                if status not in ['accepted','implemented','cancelled']:raise Denied('invalid_status','Karar durumu geçersiz.',422)
                c.execute('INSERT INTO board_decisions(meeting_date,title,decision,status,created_by) VALUES(%s,%s,%s,%s,%s)',(meeting_date,title,decision,status,uid));audit(c,uid,'decision.create',title,{'status':status})
            return {'items':c.execute('SELECT d.*,u.name created_by_name FROM board_decisions d LEFT JOIN platform_users u ON u.id=d.created_by ORDER BY meeting_date DESC,d.id DESC LIMIT 500').fetchall()}
        match=re.fullmatch(r'decisions/(\d+)',path)
        if match and method in ['PATCH','DELETE']:
            board(u);item=c.execute('SELECT * FROM board_decisions WHERE id=%s',(int(match[1]),)).fetchone()
            if not item:raise Denied('decision_missing','YK kararı bulunamadı.',404)
            if method=='DELETE':
                c.execute('DELETE FROM board_decisions WHERE id=%s',(item['id'],));audit(c,uid,'decision.delete',str(item['id']));return {'ok':True}
            title=str(b.get('title',item['title'])).strip();decision=str(b.get('decision',item['decision'])).strip();meeting_date=b.get('meeting_date',item['meeting_date']);status=b.get('status',item['status'])
            if not title or len(title)>220 or not decision or len(decision)>10000 or not meeting_date:raise Denied('invalid_decision','Toplantı tarihi, başlık ve karar metni gereklidir.',422)
            if status not in ['accepted','implemented','cancelled']:raise Denied('invalid_status','Karar durumu geçersiz.',422)
            c.execute('UPDATE board_decisions SET meeting_date=%s,title=%s,decision=%s,status=%s,updated_at=now() WHERE id=%s',(meeting_date,title,decision,status,item['id']));audit(c,uid,'decision.update',str(item['id']),{'status':status});return {'ok':True}
        if path=='approvals':
            board(u);rows=c.execute("SELECT id,kind,preview,status,created_at,expires_at FROM action_approvals WHERE user_id=%s ORDER BY created_at DESC LIMIT 100",(uid,)).fetchall()
            for r in rows:r['preview']=decrypt(r['preview'])
            return {'items':rows}
        match=re.fullmatch(r'approvals/([a-f0-9-]{36})',path)
        if match and method=='POST':
            board(u);a=c.execute("SELECT * FROM action_approvals WHERE id=%s AND user_id=%s AND status='pending' AND expires_at>now() FOR UPDATE",(match[1],uid)).fetchone()
            if not a:raise Denied('approval_missing','İşlem artık onaylanabilir durumda değil.',409)
            decision='approved' if b.get('approved') is True else 'denied';c.execute('UPDATE action_approvals SET status=%s WHERE id=%s',(decision,a['id']));audit(c,uid,'action.'+decision,a['id']);return {'ok':True}
        if path=='system/actions' and method=='POST':
            owner(u);action=b.get('action')
            import subprocess
            if action=='pause':c.execute("UPDATE settings SET value=jsonb_set(value,'{enabled}','false') WHERE key='limits'")
            elif action=='stop_jobs':c.execute("UPDATE jobs SET cancel_requested=true,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE status IN ('queued','running')")
            elif action=='backup':subprocess.run(['systemctl','--user','start','--no-block','inovens-platform-backup.service'],check=True,timeout=10)
            elif action=='restart_router':subprocess.run(['systemctl','--user','restart','inovens-router-general.service'],check=True,timeout=20)
            else:raise Denied('invalid_action','Sistem işlemi geçersiz.',422)
            audit(c,uid,'system.'+action);return {'ok':True}
        if path=='system':
            owner(u);from system_status import snapshot
            return snapshot(c)
        if path=='incidents':
            if u['role']!='owner':raise Denied('owner_required','Hata Merkezi yalnız proje sahibine açıktır.')
            rows=c.execute("""SELECT i.*,(SELECT jsonb_build_object('id',r.id,'status',r.status,'model',r.model,'plan',r.plan,'evidence',r.evidence,'error',r.error,'created_at',r.created_at,'finished_at',r.finished_at) FROM repair_runs r WHERE r.incident_id=i.id ORDER BY r.created_at DESC LIMIT 1) last_repair FROM incidents i ORDER BY (i.status IN ('open','fixing','needs_review')) DESC,i.last_seen DESC LIMIT 200""").fetchall()
            return {'items':rows,'open':sum(1 for x in rows if x['status'] in ['open','fixing','needs_review']),'fixer_model':'inovens-panel-fixer'}
        if path=='incidents/self-test' and method=='POST':
            if u['role']!='owner':raise Denied('owner_required','Hata Merkezi yalnız proje sahibine açıktır.')
            from incident_center import record
            iid=record('self-test','self_test','Hata Merkezi uçtan uca doğrulama kaydı.',{'requested_by':'owner'},'info',connection=c,fingerprint_key=str(uuid.uuid4()))
            rid=str(uuid.uuid4());c.execute("INSERT INTO repair_runs(id,incident_id,requested_by) VALUES(%s,%s,%s)",(rid,iid,uid));c.execute("UPDATE incidents SET status='fixing' WHERE id=%s",(iid,));audit(c,uid,'incident.self_test',iid,{'repair_id':rid});return {'incident_id':iid,'repair_id':rid}
        match=re.fullmatch(r'incidents/([a-f0-9-]{36})/fix',path)
        if match and method=='POST':
            if u['role']!='owner':raise Denied('owner_required','Hata Merkezi yalnız proje sahibine açıktır.')
            incident=c.execute('SELECT * FROM incidents WHERE id=%s FOR UPDATE',(match[1],)).fetchone()
            if not incident:raise Denied('incident_missing','Hata kaydı bulunamadı.',404)
            active=c.execute("SELECT id FROM repair_runs WHERE incident_id=%s AND status IN ('queued','analyzing','applying','verifying')",(incident['id'],)).fetchone()
            if active:return {'repair_id':str(active['id']),'already_running':True}
            rid=str(uuid.uuid4());c.execute('INSERT INTO repair_runs(id,incident_id,requested_by) VALUES(%s,%s,%s)',(rid,incident['id'],uid));c.execute("UPDATE incidents SET status='fixing' WHERE id=%s",(incident['id'],));audit(c,uid,'incident.repair_requested',incident['id'],{'repair_id':rid});return {'repair_id':rid}
        if path=='audit':
            owner(u);return {'items':c.execute('SELECT * FROM audit_events ORDER BY id DESC LIMIT 100').fetchall()}
        if path=='privacy/delete' and method=='POST':
            if b.get('confirm')!='SİL':raise Denied('confirmation_required','Silme işlemi için SİL yazın.',422)
            c.execute('UPDATE jobs SET cancel_requested=true WHERE user_id=%s',(uid,));c.execute('UPDATE platform_users SET workspace_epoch=workspace_epoch+1 WHERE id=%s',(uid,));c.execute('INSERT INTO deletion_tombstones(user_id,kind,target) VALUES(%s,%s,%s)',(uid,'all',str(uid)));c.execute('UPDATE jobs SET prompt=%s WHERE user_id=%s',(encrypt({}),uid));c.execute('DELETE FROM conversations WHERE user_id=%s',(uid,));c.execute('DELETE FROM share_messages WHERE sender_id=%s',(uid,));c.execute("UPDATE document_shares SET status='revoked',updated_at=now() WHERE to_user=%s",(uid,));c.execute('DELETE FROM documents WHERE owner_id=%s',(uid,));audit(c,uid,'privacy.delete');return {'ok':True}
        raise Denied('not_found','İşlem bulunamadı.',404)

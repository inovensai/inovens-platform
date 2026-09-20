"""Credentials stay here. Workers can request only identity-scoped operations."""
import importlib.util, threading, uuid, base64, ipaddress, socket, urllib.parse, httpx, re, mimetypes, smtplib, ssl, html
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from common import *
from portal import add_document, search_documents
import archive
GOOGLE_LOCK=threading.RLock()
MAX_ATTACHMENT_BYTES=25_000_000

def google_permission(u):
    board(u)

def google_module():
    spec=importlib.util.spec_from_file_location('legacy_google','/home/ege/.openclaw-inovens/brain-admin/google_mcp.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def send_pending_invitation():
    """Send one trusted onboarding email; ambiguous sends are never retried automatically."""
    with connect() as c:
        invitation=c.execute("SELECT i.*,u.name inviter_name FROM invitations i JOIN platform_users u ON u.id=i.created_by WHERE i.email_status='pending' ORDER BY i.created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not invitation:return
        c.execute("UPDATE invitations SET email_status='sending',email_error=NULL WHERE email=%s",(invitation['email'],))
    roles={'member':'Topluluk üyesi','board':'Yönetim kurulu üyesi','president':'Başkan','vice_president':'Başkan yardımcısı'}
    body=f"""Merhaba,

{invitation['inviter_name']} sizi INOVENS'AI sistemine {roles.get(invitation['role'],invitation['role'])} olarak davet etti.

Başlamak için:
1. https://inovensai.com/panel/ adresini açın.
2. {invitation['email']} Google hesabıyla giriş yapın. Davetiniz bulunduğu için hesabınız otomatik etkinleşir.
3. Panelde Hesabım sayfasını açıp Bağlantı oluştur düğmesine basın.
4. Oluşturulan bağlantıyla Telegram'da @inovens_bot hesabını açın ve bağlantıyı tamamlayın.

Telegram kullanımı:
• /kisisel — yalnız size ait kişisel çalışma alanı
• /topluluk — topluluk çalışmaları alanı (rolünüz izin veriyorsa)
• Fotoğraf ve dosyaları doğrudan Telegram sohbetine yükleyip bunlar hakkında soru sorabilirsiniz.

Bağlantı kişiye özeldir; başka biriyle paylaşmayın. Bir sorun yaşarsanız inovensai@gmail.com adresine yazabilirsiniz.

İyi çalışmalar,
INOVENS'AI"""
    try:
        message=EmailMessage();message['From']="INOVENS'AI <"+os.environ['INVITATION_SMTP_USER']+'>';message['To']=invitation['email'];message['Reply-To']='info@inovensai.com';message['Subject']="INOVENS'AI sistemine davet edildiniz";message.set_content(body)
        with smtplib.SMTP_SSL(os.environ['INVITATION_SMTP_HOST'],int(os.environ.get('INVITATION_SMTP_PORT','465')),context=ssl.create_default_context(),timeout=30) as smtp:
            smtp.login(os.environ['INVITATION_SMTP_USER'],os.environ['INVITATION_SMTP_PASSWORD']);refused=smtp.send_message(message)
        if refused:raise RuntimeError('recipient_refused')
        status,error='sent',None
    except Exception as exc:status,error='uncertain',type(exc).__name__
    with connect() as c:
        c.execute('UPDATE invitations SET email_status=%s,email_sent_at=CASE WHEN %s=%s THEN now() ELSE NULL END,email_error=%s WHERE email=%s',(status,status,'sent',error,invitation['email']))
        audit(c,invitation['created_by'],'invitation.email_'+status,invitation['email'],{'role':invitation['role'],'error':error})

def public_url(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme not in ['http','https'] or not p.hostname or p.username or p.password or p.port not in [None,80,443]:raise Denied('url_denied','Yalnız herkese açık HTTP/HTTPS adresleri kullanılabilir.')
    addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise Denied('url_denied','Yerel ve özel ağ adreslerine erişilemez.')
    addresses.sort(key=lambda a:a[0]!=socket.AF_INET)
    return p,addresses[0][4][0]

def web_fetch(url):
    # Pin the validated public address; Host/SNI retain the original authority. Revalidate each redirect.
    for _ in range(5):
        p,ip=public_url(url);authority=('['+ip+']') if ':' in ip else ip
        pinned=urllib.parse.urlunsplit((p.scheme,authority,p.path or '/',p.query,''))
        try:
            with httpx.Client(trust_env=False,timeout=25,follow_redirects=False) as client:
                with client.stream('GET',pinned,headers={'Host':p.netloc,'User-Agent':'Mozilla/5.0 (compatible; INOVENS-Research/2.1)'},extensions={'sni_hostname':p.hostname.encode()}) as r:
                    if r.is_redirect:url=urllib.parse.urljoin(url,r.headers['location']);continue
                    if r.status_code>=400:
                        return {'available':False,'recoverable':True,'http_status':r.status_code,'attempted_url':url,'message':'Bu kaynak okunamadı; arama sonuçlarındaki başka bir kaynağı açın.'}
                    data=b''
                    for part in r.iter_bytes():
                        data+=part
                        if len(data)>1_000_000:break
                    text=data.decode('utf-8',errors='replace');text=re.sub(r'<(script|style)[^>]*>.*?</\1>','',text,flags=re.S|re.I);text=re.sub('<[^>]+>',' ',text);text=re.sub(r'\s+',' ',text)
                    return {'url':url,'text':text[:14000],'trust':'untrusted_source','truncated':len(text)>14000}
        except (httpx.HTTPError,OSError):
            return {'available':False,'recoverable':True,'attempted_url':url,'message':'Bu kaynağa şu anda ulaşılamadı; başka bir kaynakla devam edin.'}
    raise Denied('redirect_limit','Çok fazla yönlendirme.',422)

def web_search(query):
    query=str(query or '').strip()[:500]
    if len(query)<2:raise Denied('search_query_required','Arama sorgusu gerekli.',422)
    url='https://html.duckduckgo.com/html/?'+urllib.parse.urlencode({'q':query,'kl':'tr-tr'})
    results=[]
    try:
        with httpx.Client(trust_env=False,timeout=15,follow_redirects=True,headers={'User-Agent':'Mozilla/5.0'}) as client:r=client.get(url)
        if r.status_code<400:
            pattern=r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            for match in re.finditer(pattern,r.text,re.I|re.S):
                target=html.unescape(match.group(1));title=re.sub(r'<[^>]+>',' ',html.unescape(match.group(2)));title=re.sub(r'\s+',' ',title).strip()
                parsed=urllib.parse.urlsplit(target)
                if parsed.hostname and parsed.hostname.endswith('duckduckgo.com'):target=urllib.parse.parse_qs(parsed.query).get('uddg',[target])[0]
                _append_search_result(results,title,target)
    except Exception:pass
    # DuckDuckGo sometimes serves an anti-bot page with HTTP 202. RSS sources give
    # a stable, parseable fallback and keep one provider failure from killing a job.
    rss_urls=[
        'https://www.bing.com/search?'+urllib.parse.urlencode({'format':'rss','q':query}),
        'https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':query,'hl':'tr','gl':'TR','ceid':'TR:tr'}),
    ]
    for rss_url in rss_urls:
        if len(results)>=10:break
        try:
            with httpx.Client(trust_env=False,timeout=15,follow_redirects=True,headers={'User-Agent':'Mozilla/5.0'}) as client:r=client.get(rss_url)
            if r.status_code>=400:continue
            root=ET.fromstring(r.content)
            for item in root.findall('.//item'):
                _append_search_result(results,item.findtext('title') or '',item.findtext('link') or '')
                if len(results)>=10:break
        except Exception:continue
    if not results:return {'query':query,'results':[],'available':False,'recoverable':True,'message':'Bu sorgu sonuç vermedi; sorguyu sadeleştirip yeniden arayın.'}
    return {'query':query,'results':results[:10],'trust':'untrusted_source','providers_tried':['duckduckgo','bing_rss','google_news_rss']}

def _append_search_result(results,title,target):
    title=re.sub(r'\s+',' ',html.unescape(str(title))).strip();target=html.unescape(str(target)).strip()
    if not title or not target or any(item['url']==target for item in results):return
    try:public_url(target)
    except Exception:return
    results.append({'title':title[:240],'url':target})

def import_workspace_file(c,u,j,path,display_name=None):
    """Move a worker-created file into encrypted platform storage without trusting host paths."""
    if not isinstance(path,str) or not path.strip():raise Denied('file_required','Geçerli bir çalışma alanı dosyası gerekli.',422)
    relative=path.strip()
    if relative.startswith('/workspace/'):relative=relative[len('/workspace/'):]
    elif relative=='/workspace':relative=''
    else:relative=relative.lstrip('/')
    base=(ROOT/'users'/str(u['id'])/'personal-worker'/str(u.get('workspace_epoch',0))/'workspace').resolve()
    candidate=(base/relative).resolve()
    try:candidate.relative_to(base)
    except ValueError:raise Denied('file_denied','Yalnız kendi çalışma alanınızdaki dosyalar kullanılabilir.',403)
    if not candidate.is_file() or candidate.is_symlink():raise Denied('file_missing','Çalışma alanı dosyası bulunamadı.',422)
    size=candidate.stat().st_size
    if size<=0 or size>MAX_ATTACHMENT_BYTES:raise Denied('file_size','Dosya boş veya 25 MB sınırını aşıyor.',422)
    raw=candidate.read_bytes();name=Path(display_name or candidate.name).name[:180]
    if not name:raise Denied('file_required','Dosya adı geçersiz.',422)
    mime=mimetypes.guess_type(name)[0] or 'application/octet-stream'
    from extraction import extract
    text=extract(raw,name,mime)
    from data_policy import guard_tool
    guard_tool(u,j,name+'\n'+text)
    scope='board' if j.get('scope') in ['board','google'] and u['role'] in BOARD else 'personal'
    did=add_document(c,u,name,text,scope=scope,google=bool(j['google_bound']),filedata=base64.b64encode(raw).decode(),mime=mime,generated=scope=='board')
    return did,name,size

def owned_document(c,u,did):
    return c.execute('SELECT * FROM documents WHERE id=%s AND owner_id=%s',(did,u['id'])).fetchone() if did else None

def validate_document_request(c,j,args):
    """Reject shallow research before rendering so the agent can repair it."""
    from execution_contract import from_job,source_urls
    contract=from_job(j);content=str(args.get('content',''));sources=args.get('sources',[])
    if contract.get('source_required'):
        rows=c.execute("SELECT detail FROM audit_events WHERE action='tool.used' AND target=%s",(str(j['id']),)).fetchall()
        required=contract.get('min_source_reads',3);opened=source_urls(rows)
        cited={match.group(0).rstrip('.,;:)') for value in sources if isinstance(value,str) for match in re.finditer(r'https?://[^\s<>]+',value)} if isinstance(sources,list) else set()
        if len(opened)<required:raise Denied('document_sources_unread',f'Belge için en az {required} farklı kaynak açılıp okunmalıdır; şu an {len(opened)}.',422)
        if len(cited)<required:raise Denied('document_sources_insufficient',f'Belgede en az {required} kaynak URL bulunmalıdır; şu an {len(cited)}.',422)
    expected=contract.get('expected_item_count')
    if expected:
        entries={int(m.group(1)) for m in re.finditer(r'(?m)^\s*(?:#{1,4}\s*)?(\d{1,3})[.)\]:-]\s+\S',content)}
        words=re.findall(r'\b\w+\b',content,re.UNICODE)
        if len(entries)<expected:raise Denied('document_items_missing',f'İstenen {expected} ayrı kaydın yalnız {len(entries)} tanesi belgede bulundu.',422)
        if len(words)<expected*45:raise Denied('document_content_shallow',f'{expected} kayıt için açıklama ve analiz yetersiz; en az {expected*45} sözcüklük anlamlı içerik gerekli.',422)

def call(u,j,name,a):
    with connect() as c:
        u=user(c,u['id'],consent=True)
        current=c.execute('SELECT j.*,cv.google_bound,cv.scope FROM jobs j JOIN conversations cv ON cv.id=j.conversation_id WHERE j.id=%s AND j.user_id=%s',(j['id'],u['id'])).fetchone()
        if not current or current['status']!='running' or current['cancel_requested']:raise Denied('job_stopped','İş durduruldu.')
        j=current
        if name=='knowledge_search':
            from retrieval_policy import allows_archive
            if not allows_archive(j):raise Denied('archive_not_requested','Bu istek arşiv gerektirmiyor. Mevcut mesajla doğrudan yanıt ver; aramayı tekrarlama.')
        if name=='knowledge_search' and j['google_bound']:google_permission(u)
        if name=='knowledge_search':
            items=search_documents(c,u,str(a.get('query',''))[:500],google_allowed=u['role'] in BOARD)
            items=items[:3]
            from data_policy import guard_tool
            for did in {item['id'] for item in items}:
                document=c.execute('SELECT name,body,generated FROM documents WHERE id=%s',(did,)).fetchone()
                body=decrypt(document['body'])
                if document['generated']:
                    from data_policy import guard_generated_context
                    guard_generated_context(u,j,document['name']+'\n'+body.get('text',''))
                else:guard_tool(u,j,document['name']+'\n'+body.get('text',''))
                if body.get('file'):
                    guard_tool(u,j,{'opaque_attachment':True})
            remaining=6000
            for item in items:
                item['text']=item['text'][:remaining];remaining-=len(item['text'])
            if any(x['google_source'] for x in items):c.execute('UPDATE conversations SET google_bound=true WHERE id=%s',(j['conversation_id'],))
            return {'items':items}
        if name=='save_memory':
            from data_policy import guard_tool
            guard_tool(u,j,a)
            memory_scope='board' if j.get('scope') in ['board','google'] and u['role'] in BOARD else 'personal'
            kind=a.get('kind','note');metadata={'tags':a.get('tags',[]),'status':'active'}
            if a.get('due_at'):metadata['due_at']=a['due_at']
            did=archive.create_memory(c,u,str(a.get('name','Not'))[:180],str(a.get('text',''))[:50000],kind,metadata,memory_scope)
            return {'id':did,'scope':memory_scope}
        if name=='archive_find':
            result=archive.list_documents(c,u,str(a.get('query',''))[:120],a,limit=10)
            return {'items':[{'id':str(x['id']),'name':x['name'],'scope':x['scope'],'kind':x['archive_kind'],'metadata':x['archive_meta'],'review_status':x['review_status']} for x in result['items']]}
        if name=='archive_update':
            d=c.execute('SELECT * FROM documents WHERE id=%s AND owner_id=%s FOR UPDATE',(str(a.get('document_id','')),u['id'])).fetchone()
            if not d:raise Denied('document_owner','Yalnız kendi belgenizi sınıflandırabilirsiniz.',403)
            meta=archive.validated_metadata({k:a[k] for k in ('academic_year','grade','semester','course','exam_type','tags') if k in a})
            kind=str(a.get('kind','general'))
            if kind not in archive.KINDS:raise Denied('archive_kind_invalid','Arşiv türü geçersiz.',422)
            review='submitted' if d['review_status']=='submitted' else 'ready'
            c.execute('UPDATE documents SET archive_kind=%s,archive_meta=archive_meta||%s::jsonb,review_status=%s WHERE id=%s',(kind,dumps(meta),review,d['id']))
            audit(c,u['id'],'document.ai_classified',str(d['id']),{'kind':kind,'fields':list(meta)})
            return {'id':str(d['id']),'kind':kind,'metadata':meta,'review_status':review}
        if name=='archive_share':
            prompt=decrypt(j['prompt']).get('text','')
            recipient=str(a.get('recipient','')).strip()
            if not re.search(r'paylaş|paylas|share',prompt,re.I) or recipient.casefold() not in prompt.casefold():
                raise Denied('share_intent_missing','Paylaşım için kullanıcının açık isteği ve seçtiği alıcı gerekli.',422)
            return archive.create_share(c,u,str(a.get('document_id','')),recipient,a.get('message',''))
        if name=='share_inbox':
            return {'items':[{'id':str(x['id']),'document_id':str(x['document_id']),'name':x['name'],'status':x['status'],'from_name':x['from_name'],'to_name':x['to_name']} for x in archive.list_shares(c,u)['items']]}
        if name=='share_message':
            prompt=decrypt(j['prompt']).get('text','')
            if not re.search(r'mesaj|ileti|yaz|reply|message',prompt,re.I):raise Denied('message_intent_missing','Paylaşım mesajı için kullanıcının açık isteği gerekli.',422)
            return archive.send_message(c,u,str(a.get('share_id','')),a.get('text',''))
        if name=='document_create':
            from document_factory import create
            validate_document_request(c,j,a)
            result=create(u,j,a)
            from data_policy import guard_tool
            guard_tool(u,j,{'filename':result['filename'],'format':result['format'],'verified':True})
            return result
        if name=='image_create':
            from image_factory import create
            result=create(u,j,a)
            from data_policy import guard_tool
            guard_tool(u,j,{'filename':result['filename'],'format':result['format'],'verified':True})
            return result
        if name in ['github_read','github_write']:
            owner(u)
            from github_broker import call as github_call
            result=github_call(name,a)
            from data_policy import guard_tool
            guard_tool(u,j,result)
            return result
        if name in ['web_fetch','web_search','browser_open','browser_read','browser_click']:
            if name=='web_fetch':return web_fetch(str(a.get('url','')))
            if name=='web_search':return web_search(a.get('query',''))
            from browser_broker import call as browse
            try:return browse(u['id'],name,a)
            except Exception:
                return {'available':False,'recoverable':True,'message':'Araştırma tarayıcısı zamanında yanıt vermedi; web_search veya başka bir kaynakla devam edin.'}
        if name not in ['google_status','drive_search','drive_download','gmail_search','gmail_send','drive_upload','calendar_upcoming']:raise Denied('tool_denied','Araç izinli değil.')
        google_permission(u)
        # Preserve Google provenance before returning content, including egress restrictions.
        c.execute('UPDATE conversations SET google_bound=true WHERE id=%s',(j['conversation_id'],))
        if name in ['gmail_send','drive_upload']:
            aid=str(uuid.uuid4());expires=now()+timedelta(minutes=15)
            if name=='gmail_send':
                recipients=a.get('to',[])
                if not isinstance(recipients,list) or not 1<=len(recipients)<=25 or any(not isinstance(v,str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',v) for v in recipients):raise Denied('invalid_mail','Geçerli alıcı listesi gerekli.',422)
                for k in ['subject','body']:
                    if not isinstance(a.get(k),str) or not a[k].strip():raise Denied('invalid_mail','Alıcı, konu ve ileti metni gerekli.',422)
                document_ids=a.get('documentIds',[]);paths=a.get('attachments',[])
                if not isinstance(document_ids,list) or not isinstance(paths,list) or len(document_ids)+len(paths)>10:raise Denied('invalid_attachments','En fazla on ek seçilebilir.',422)
                resolved=[];attachment_names=[];total=0
                for did in document_ids:
                    d=owned_document(c,u,did)
                    if not d:raise Denied('document_missing','Eklenecek belgelerden biri bulunamadı.',422)
                    body=decrypt(d['body']);raw=base64.b64decode(body['file']) if body.get('file') else body.get('text','').encode()
                    total+=len(raw);resolved.append(str(d['id']));attachment_names.append(d['name'])
                for path in paths:
                    did,filename,size=import_workspace_file(c,u,j,path)
                    total+=size;resolved.append(did);attachment_names.append(filename)
                if total>MAX_ATTACHMENT_BYTES:raise Denied('attachments_size','Eklerin toplam boyutu 25 MB sınırını aşıyor.',422)
                payload={k:a[k] for k in ['to','cc','bcc','subject','body'] if k in a};payload['documentIds']=resolved
                preview={k:v for k,v in payload.items() if k!='documentIds'};preview['attachments']=attachment_names
            else:
                did=a.get('documentId');d=owned_document(c,u,did)
                if not d and a.get('localPath'):
                    did,_,_=import_workspace_file(c,u,j,a['localPath'],a.get('name'));d=owned_document(c,u,did)
                if not d:raise Denied('document_required','Yükleme için documentId veya /workspace altındaki localPath gerekli.',422)
                payload={k:a[k] for k in ['parentFolderId','replaceFileId','ifVersion','convertTo'] if k in a};payload.update(documentId=str(d['id']),name=d['name']);preview=payload
            c.execute('INSERT INTO action_approvals(id,user_id,job_id,kind,preview,payload,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s)',(aid,u['id'],j['id'],name,encrypt(preview),encrypt(payload),expires))
            return {'approval_id':aid,'status':'pending','message':'İşlem yapılmadı. Kullanıcı panelde Onaylar bölümündeki önizlemeyi onaylamalı.','expires_at':expires}
    with GOOGLE_LOCK:
        mod=google_module();folder=ROOT/'users'/str(u['id'])/'google';folder.mkdir(parents=True,exist_ok=True)
        mod.WORKSPACE=folder;mod.DOWNLOADS=folder/'knowledge/drive';mod.DOWNLOADS.mkdir(parents=True,exist_ok=True)
        safe={k:v for k,v in a.items() if k in ['query','fileId','outputName','format']}
        if 'outputName' in safe:safe['outputName']=Path(safe['outputName']).name
        result=mod.call(name,safe)
        from data_policy import guard_tool
        guard_tool(u,j,result)
        if name=='drive_download':
            from extraction import extract
            docs=[]
            for f in mod.DOWNLOADS.iterdir():
                if f.is_file() and f.stat().st_size<=25_000_000:
                    raw=f.read_bytes()
                    extracted=extract(raw,f.name,'')
                    guard_tool(u,j,extracted)
                    with connect() as c:
                        uid=add_document(c,user(c,u['id']),f.name,extracted,google=True,filedata=base64.b64encode(raw).decode());docs.append(uid)
                    f.unlink()
            return {'document_ids':docs,'google_source':True}
        return {'source':'community_google','google_source':True,'content':dumps(result)[:14000]}

def execute_approvals():
    with connect() as c:
        a=c.execute("SELECT * FROM action_approvals WHERE status='approved' AND expires_at>now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not a:return
        try:u=user(c,a['user_id'],consent=True);google_permission(u)
        except Denied:
            c.execute("UPDATE action_approvals SET status='denied' WHERE id=%s",(a['id'],));return
        # A crash after this commit is 'uncertain'; never resend automatically.
        c.execute("UPDATE action_approvals SET status='executing' WHERE id=%s",(a['id'],))
    try:
        payload=decrypt(a['payload'])
        with GOOGLE_LOCK:
            mod=google_module();folder=ROOT/'users'/str(u['id'])/'google';folder.mkdir(parents=True,exist_ok=True);mod.WORKSPACE=folder
            staged=[];stage_dir=folder/('.action-'+str(a['id']));stage_dir.mkdir(mode=0o700,exist_ok=False)
            try:
                if a['kind']=='drive_upload':
                    with connect() as c:d=c.execute('SELECT * FROM documents WHERE id=%s AND owner_id=%s',(payload.pop('documentId'),u['id'])).fetchone()
                    if not d:raise Denied('document_missing','Belge silinmiş.')
                    content=decrypt(d['body']);path=stage_dir/Path(d['name']).name;path.write_bytes(base64.b64decode(content['file']) if content.get('file') else content['text'].encode());staged.append(path);payload['localPath']=str(path)
                elif a['kind']=='gmail_send':
                    ids=payload.pop('documentIds',[]);payload['attachments']=[]
                    for index,did in enumerate(ids):
                        with connect() as c:d=c.execute('SELECT * FROM documents WHERE id=%s AND owner_id=%s',(did,u['id'])).fetchone()
                        if not d:raise Denied('document_missing','Eklenecek belge silinmiş.')
                        filename=Path(d['name']).name
                        if any(path.name==filename for path in staged):filename=str(index)+'-'+filename
                        content=decrypt(d['body']);path=stage_dir/filename;path.write_bytes(base64.b64decode(content['file']) if content.get('file') else content['text'].encode());staged.append(path);payload['attachments'].append(str(path))
                result=mod.call(a['kind'],payload)
            finally:
                for path in staged:path.unlink(missing_ok=True)
                stage_dir.rmdir()
        if result.get('ok') is False or (isinstance(result.get('data'),dict) and result['data'].get('ok') is False):raise RuntimeError('google_action_failed')
        status='done';code='completed'
    except Exception:status='uncertain';code='execution_result_unknown'
    with connect() as c:
        c.execute('UPDATE action_approvals SET status=%s,executed_at=now(),result=%s WHERE id=%s',(status,encrypt({'code':code}),a['id']));audit(c,u['id'],'action.'+status,a['id'])

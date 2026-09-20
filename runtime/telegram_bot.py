import httpx,time,hashlib,uuid,re,mimetypes
from common import *
from portal import enqueue,add_document
import archive

CLIENT=httpx.Client(trust_env=False,limits=httpx.Limits(max_connections=12,max_keepalive_connections=8))
MAX_INCOMING_BYTES=20_000_000

def api(method,body):
    timeout=httpx.Timeout(30,connect=5) if method=='getUpdates' else httpx.Timeout(10,connect=5)
    r=CLIENT.post('https://api.telegram.org/bot'+os.environ['TELEGRAM_TOKEN']+'/'+method,json=body,timeout=timeout);r.raise_for_status();data=r.json()
    if not data.get('ok'):raise RuntimeError('telegram_failed')
    return data['result']

def send(chat,text,reply_markup=None):
    chunks=[text[start:start+3800] for start in range(0,len(text),3800)] or ['']
    for index,chunk in enumerate(chunks):
        body={'chat_id':chat,'text':chunk}
        if reply_markup and index==len(chunks)-1:body['reply_markup']=reply_markup
        api('sendMessage',body)

MENU={'inline_keyboard':[
    [{'text':'💬 Kişisel','callback_data':'cmd:/kisisel'},{'text':'👥 Topluluk','callback_data':'cmd:/topluluk'}],
    [{'text':'🗂 Arşivim','callback_data':'cmd:/arsiv'},{'text':'💡 Fikirler','callback_data':'cmd:/fikirler'}],
    [{'text':'⏰ Hatırlatmalar','callback_data':'cmd:/hatirlatmalar'},{'text':'🤝 Paylaşımlar','callback_data':'cmd:/paylasimlar'}],
    [{'text':'❓ Yardım','callback_data':'cmd:/yardim'}],
]}
CONSENT_TEXT="""INOVENS'AI Telegram kullanımı\n\n• Mesajlarınız yanıt üretmek için seçilen yapay zekâ/model sağlayıcılarına gönderilebilir; sağlayıcılar Türkiye dışında işlem yapabilir.\n• Genel ve hassas olmayan içerik Contributor rotasında işlenebilir. Hassas içerik ayrı rotaya yönlendirilir; sistem sağlayıcının sıfır saklama garantisi olduğunu iddia etmez.\n• Sohbet ve dosyalar ev sunucusunda yetkinize göre saklanır. Kişisel alanınız diğer kullanıcılara kapalıdır; paylaşım ancak sizin seçtiğiniz alıcı kabul edince açılır.\n• Şifre, erişim anahtarı, kimlik, sağlık veya finans verisi yüklemeyin.\n• Ayrıntılı açıklama ve verileri silme/dışa aktarma: https://inovensai.com/panel\n\nDevam ederek bu bilgilendirmeyi okuduğunuzu ve Telegram üzerinden işlem yapılmasını kabul ettiğinizi belirtirsiniz."""

def consent_prompt(chat):
    send(chat,CONSENT_TEXT,{'inline_keyboard':[[{'text':'✅ Okudum ve kabul ediyorum','callback_data':'consent:accept'}],[{'text':'❌ Kabul etmiyorum','callback_data':'consent:decline'}]]})

def help_text(role):
    sections=["""INOVENS'AI kullanım rehberi

Botla normal bir insanla konuşur gibi yazabilirsiniz. Komut kullanmak zorunlu değildir; komutlar sık kullanılan işlemleri hızlandırır.

ÇALIŞMA ALANLARI
/kisisel — yalnız size ait sohbet, dosya, fikir ve notlar
/topluluk — üyelerin ortak topluluk bilgi alanı
/alan — şu anda hangi alanda olduğunuzu gösterir"""]
    if role in BOARD:sections[0]+='\n/yk — yalnız yetkili yönetim kullanıcılarının alanı'
    sections.append("""DOSYA VE ARŞİV
Fotoğraf veya dosyayı doğrudan sohbete gönderin. Aynı mesaja ne istediğinizi yazabilirsiniz.
Örnek: “EEM 2. sınıf 2. dönem 2025-2026 Elektrik Makineleri vize sorusu; arşivle ve çöz.”
/arsiv [arama] — erişebildiğiniz dosyaları bulur
Örnek: /arsiv elektrik makineleri vize

FİKİR, NOT VE HATIRLATMA
/fikir <metin> — kişisel fikir kaydeder
/fikirler — fikirlerinizi listeler
/not <metin> — kişisel not kaydeder
/hatirlat YYYY-AA-GG SS:DD <metin> — zamanlı hatırlatma ekler
/hatirlatmalar — yaklaşan hatırlatmaları listeler
Doğal dil örneği: “Bunu fikir olarak kaydet” veya “Pazartesi 14.00'te başvuru formunu hatırlat.”""")
    sections.append("""PAYLAŞIM VE SESSİZ MESAJLAŞMA
Kişisel bir dosyanızı seçtiğiniz sistem kullanıcısıyla paylaşabilirsiniz. Alıcı kabul etmeden içeriği göremez.
/paylasimlar — gelen ve giden paylaşım davetlerini gösterir
Doğal dil örneği: “Devre Analizi notumu Yusuf Kaya ile paylaş.”
Kabul edilmiş paylaşım için: “Devre Analizi paylaşımında Yusuf'a ‘3. soruya bakar mısın?’ yaz.”
Bu mesajlar yalnız ilgili paylaşım konuşmasında görünür ve Telegram bildirimi göndermez. Panelde Arşiv ve hafıza → Paylaşım kutusu → Sessiz konuşmayı aç yoluyla da yazabilirsiniz.""")
    sections.append("""İŞ VE MODEL KONTROLÜ
/new — yeni konuşma
/jobs — devam eden işleri gösterir
/cancel — devam eden işi durdurur
/quota — kullanım durumunu gösterir
/model fast|smart|code — kullanılacak modeli seçer
/menu — hızlı erişim düğmelerini açar

Şifre, erişim anahtarı, kimlik, sağlık veya finans bilgisi göndermeyin. Ayrıntılı veri açıklaması ve hesap işlemleri: https://inovensai.com/panel""")
    return '\n\n'.join(sections)

def archive_items(c,u,kind=None,query=''):
    result=archive.list_documents(c,u,query,{'kind':kind} if kind else {},limit=10)
    if not result['items']:return 'Eşleşen kayıt bulunamadı.'
    return '\n'.join('• '+x['name']+(' · '+x['archive_meta'].get('due_at','')[:16].replace('T',' ') if x['archive_meta'].get('due_at') else '') for x in result['items'])

def send_attachment(chat,name,raw,mime):
    image=mime.startswith('image/') and len(raw)<=10_000_000
    method,field=('sendPhoto','photo') if image else ('sendDocument','document')
    r=CLIENT.post('https://api.telegram.org/bot'+os.environ['TELEGRAM_TOKEN']+'/'+method,data={'chat_id':str(chat)},files={field:(Path(name).name,raw,mime)},timeout=httpx.Timeout(120,connect=10));r.raise_for_status();data=r.json()
    if not data.get('ok'):raise RuntimeError('telegram_attachment_failed')
    return data['result']

def incoming_attachment(message):
    """Download one private-chat photo or document without exposing the bot token."""
    document=message.get('document')
    photos=message.get('photo') if isinstance(message.get('photo'),list) else []
    if document:
        item=document;fallback='telegram-dosyasi'
        supplied=str(item.get('file_name') or fallback)
        name=Path(supplied).name.replace('\x00','').strip()[:180] or fallback
        mime=str(item.get('mime_type') or mimetypes.guess_type(name)[0] or 'application/octet-stream')[:180]
    elif photos:
        item=photos[-1];name='telegram-fotograf-'+str(item.get('file_unique_id') or item.get('file_id',''))[:40]+'.jpg';mime='image/jpeg'
    else:return None
    declared=int(item.get('file_size') or 0)
    if declared>MAX_INCOMING_BYTES:raise Denied('file_too_large','Telegram üzerinden en fazla 20 MB dosya yükleyebilirsiniz.',422)
    info=api('getFile',{'file_id':item['file_id']});remote=str(info.get('file_path') or '')
    if not remote or '..' in Path(remote).parts:raise Denied('telegram_file_invalid','Telegram dosyası alınamadı.',502)
    response=CLIENT.get('https://api.telegram.org/file/bot'+os.environ['TELEGRAM_TOKEN']+'/'+remote,timeout=httpx.Timeout(120,connect=10));response.raise_for_status()
    raw=response.content
    if not raw or len(raw)>MAX_INCOMING_BYTES:raise Denied('file_too_large','Telegram üzerinden en fazla 20 MB dosya yükleyebilirsiniz.',422)
    return {'name':name,'mime':mime,'raw':raw,'kind':'photo' if photos and not document else 'document'}

def direct_test_mail_preview(c,u,text,scope):
    """Create a deterministic approval for simple test-mail commands."""
    if u['role'] not in BOARD or scope not in ['board','google']:return None
    normalized=text.casefold().replace('ı','i')
    intent=re.search(r'\b(?:mail|e-?posta)\w*\b',normalized) and re.search(r'\b(?:at|gönder|gonder|yolla|ilet)\w*\b',normalized)
    recipients=list(dict.fromkeys(re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}',text)))
    if not intent or 'test' not in normalized or not 1<=len(recipients)<=25:return None
    payload={'to':recipients,'subject':'INOVENS - Test E-postası','body':'Merhaba,\n\nBu, INOVENS topluluk e-posta hesabı üzerinden gönderilen bir test iletisidir.\n\nİyi çalışmalar dileriz,\nINOVENS Ekibi'}
    aid=str(uuid.uuid4());expires=now()+timedelta(minutes=15)
    c.execute('INSERT INTO action_approvals(id,user_id,kind,preview,payload,expires_at) VALUES(%s,%s,%s,%s,%s,%s)',(aid,u['id'],'gmail_send',encrypt(payload),encrypt(payload),expires))
    audit(c,u['id'],'action.preview_created',aid,{'kind':'gmail_send','recipient_count':len(recipients),'source':'telegram_direct_test_mail'})
    return 'Test e-postası önizlemesi hazır. Henüz gönderilmedi; 15 dakika içinde https://inovensai.com/panel → Onaylar bölümünden alıcı ve içeriği kontrol edip onaylayın.'

def pairing_code(c,telegram_id):
    existing=c.execute('SELECT code FROM telegram_pair_codes WHERE telegram_id=%s AND used_at IS NULL AND expires_at>now() ORDER BY created_at DESC LIMIT 1',(telegram_id,)).fetchone()
    if existing:return existing['code']
    c.execute('DELETE FROM telegram_pair_codes WHERE telegram_id=%s',(telegram_id,))
    alphabet='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    while True:
        code=''.join(secrets.choice(alphabet) for _ in range(8))
        if not c.execute('SELECT 1 FROM telegram_pair_codes WHERE code=%s',(code,)).fetchone():break
    c.execute('INSERT INTO telegram_pair_codes(code,telegram_id,expires_at) VALUES(%s,%s,%s)',(code,telegram_id,now()+timedelta(minutes=10)))
    return code

def update(data):
    callback=data.get('callback_query')
    if callback:
        tid=callback.get('from',{}).get('id');value=str(callback.get('data',''))
        try:api('answerCallbackQuery',{'callback_query_id':callback['id']})
        except Exception:pass
        if not tid:return
        if value.startswith('consent:'):
            with connect() as c:
                u=c.execute('SELECT * FROM platform_users WHERE telegram_id=%s',(tid,)).fetchone()
                if not u:return
                if value=='consent:accept':
                    c.execute('UPDATE platform_users SET telegram_consent_version=%s,telegram_consent_at=now() WHERE id=%s',(archive.TG_CONSENT,u['id']));audit(c,u['id'],'telegram.consent_accept',archive.TG_CONSENT)
                    send(tid,'Telegram kullanımı etkin. Kişisel alanınız açık.',MENU)
                else:
                    c.execute('UPDATE platform_users SET telegram_consent_version=NULL,telegram_consent_at=NULL WHERE id=%s',(u['id'],));audit(c,u['id'],'telegram.consent_decline',archive.TG_CONSENT)
                    send(tid,'Kabul etmediğiniz için Telegram üzerinden yapay zekâ işlemi başlatılmayacak. Tercihinizi daha sonra /start yazarak değiştirebilirsiniz.')
            return
        if value.startswith('share:'):
            parts=value.split(':',2)
            if len(parts)==3:
                with connect() as c:
                    u=user(c,c.execute('SELECT id FROM platform_users WHERE telegram_id=%s',(tid,)).fetchone()['id'],consent=True)
                    result=archive.share_action(c,u,parts[2],parts[1]);send(tid,'Paylaşım durumu: '+result['status'],MENU)
            return
        if value.startswith('cmd:'):
            data={'update_id':data.get('update_id',0),'message':{'chat':{'id':tid,'type':'private'},'from':callback['from'],'text':value[4:],'date':int(time.time())}}
    m=data.get('message')
    if not m or m.get('chat',{}).get('type')!='private':return
    tid=m['from']['id'];text=m.get('text') or m.get('caption') or '';response=None
    with connect() as c:
        if text.startswith('/start '):
            digest=hashlib.sha256(text.split(' ',1)[1].encode()).hexdigest()
            link=c.execute('SELECT * FROM link_tokens WHERE hash=%s AND used_at IS NULL AND expires_at>now() FOR UPDATE',(digest,)).fetchone()
            if not link:response='Bağlantı geçersiz veya süresi dolmuş. Panelden yeni Telegram bağlantısı oluşturun.'
            else:
                u=user(c,link['user_id'],consent=True)
                existing=c.execute('SELECT id FROM platform_users WHERE telegram_id=%s AND id<>%s',(tid,u['id'])).fetchone()
                if existing:raise Denied('already_linked','Bu Telegram hesabı başka bir kullanıcıya bağlı.')
                c.execute('UPDATE platform_users SET telegram_id=%s,telegram_consent_version=NULL,telegram_consent_at=NULL WHERE id=%s',(tid,u['id']));c.execute('UPDATE link_tokens SET used_at=now() WHERE hash=%s',(digest,));audit(c,u['id'],'telegram.link');response='__CONSENT__'
        else:
            u=c.execute('SELECT * FROM platform_users WHERE telegram_id=%s',(tid,)).fetchone()
            if not u:
                code=pairing_code(c,tid)
                response='Telegram hesabınız henüz bağlı değil. https://inovensai.com/panel → Hesabım sayfasındaki “Kodla eşle” alanına şu kodu girin: '+code+'\n\nKod 10 dakika geçerlidir.'
            else:
                u=user(c,u['id'],consent=True)
                if u.get('telegram_consent_version')!=archive.TG_CONSENT:
                    consent_prompt(tid);return
                attachment=incoming_attachment(m)
                if not attachment and text.startswith('/quota'):
                    from quota import usage_view
                    q=usage_view(c,u['id']);response=('Hesabınızda günlük token kotası yok. Bugünkü kullanım: '+str(q['used'])+' token.') if q.get('unlimited') else f"Kalan: {q['remaining']:,} / {q['limit']:,} token. Her gün Türkiye saatiyle 00.00’da yenilenir."
                elif not attachment and text.startswith('/cancel'):
                    c.execute("UPDATE jobs SET cancel_requested=true,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE user_id=%s AND status IN ('queued','running')",(u['id'],));response='Durdurma isteği alındı.'
                elif not attachment and text.startswith('/model '):
                    selected='inovens-combo-'+text.split(' ',1)[1].strip().lower()
                    if selected not in MODELS:raise Denied('invalid_model','/model fast, /model smart veya /model code kullanın.')
                    c.execute('INSERT INTO telegram_preferences(user_id,model) VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE SET model=excluded.model',(u['id'],selected));response='Model seçimi: '+selected
                elif not attachment and text.startswith('/topluluk'):
                    c.execute("INSERT INTO telegram_preferences(user_id,scope) VALUES(%s,'community') ON CONFLICT(user_id) DO UPDATE SET scope=excluded.scope",(u['id'],))
                    c.execute('DELETE FROM telegram_conversations WHERE user_id=%s',(u['id'],));response='Topluluk alanına geçildi. Ortak arşiv okunabilir; yüklediğiniz katkılar YK incelemesinden sonra yayımlanır.'
                elif not attachment and text.startswith('/yk'):
                    board(u)
                    c.execute("INSERT INTO telegram_preferences(user_id,scope) VALUES(%s,'board') ON CONFLICT(user_id) DO UPDATE SET scope=excluded.scope",(u['id'],))
                    c.execute('DELETE FROM telegram_conversations WHERE user_id=%s',(u['id'],));response='YK özel alanına geçildi. Bu alan yalnız yetkili yönetim kullanıcılarına açıktır.'
                elif not attachment and text.startswith('/kisisel'):
                    c.execute("INSERT INTO telegram_preferences(user_id,scope) VALUES(%s,'personal') ON CONFLICT(user_id) DO UPDATE SET scope=excluded.scope",(u['id'],))
                    c.execute('DELETE FROM telegram_conversations WHERE user_id=%s',(u['id'],));response='Kişisel alana geçildi. Bu konuşmalar ve dosyalar diğer kullanıcılarla paylaşılmaz.'
                elif not attachment and text.startswith('/alan'):
                    pref=c.execute('SELECT scope FROM telegram_preferences WHERE user_id=%s',(u['id'],)).fetchone();scope=pref['scope'] if pref else 'personal';response='Çalışma alanı: '+{'personal':'Kişisel','community':'Topluluk','board':'YK özel'}.get(scope,scope)
                elif not attachment and text.split(' ',1)[0] in ['/help','/yardim','/yardım']:
                    response=help_text(u['role'])
                elif not attachment and text.startswith('/menu'):
                    send(tid,'Hızlı erişim',MENU);return
                elif not attachment and text.startswith('/arsiv'):
                    response=archive_items(c,u,query=text.partition(' ')[2])
                elif not attachment and text.startswith('/fikirler'):
                    response=archive_items(c,u,kind='idea')
                elif not attachment and text.startswith('/hatirlatmalar'):
                    response=archive_items(c,u,kind='reminder')
                elif not attachment and text.startswith('/paylasimlar'):
                    shares=archive.list_shares(c,u)['items']
                    pending=[x for x in shares if x['to_user']==u['id'] and x['status']=='pending']
                    for item in pending[:10]:
                        send(tid,'Paylaşım daveti: '+item['name']+'\nGönderen: '+item['from_name']+(('\nNot: '+item['introduction']) if item['introduction'] else ''),{'inline_keyboard':[[{'text':'Kabul et','callback_data':'share:accept:'+str(item['id'])},{'text':'Reddet','callback_data':'share:decline:'+str(item['id'])}]]})
                    response=('\n'.join('• '+x['name']+' · '+x['status']+' · '+(x['from_name'] if x['to_user']==u['id'] else x['to_name']) for x in shares[:15]) or 'Paylaşım bulunamadı.')+('\n\nBekleyen davetler yukarıdaki düğmelerle yönetilebilir.' if pending else '')+'\n\nKabul edilmiş bir paylaşımda mesaj bırakmak için normal şekilde “<dosya adı> paylaşımında <kişi>ye <mesaj> yaz” diyebilirsiniz. Mesaj sessiz paylaşım konuşmasına eklenir; Telegram bildirimi göndermez.'
                elif not attachment and (text.startswith('/fikir ') or text.startswith('/not ')):
                    kind='idea' if text.startswith('/fikir ') else 'note';content=text.split(' ',1)[1].strip()
                    did=archive.create_memory(c,u,content[:80],content,kind,{'status':'active'},'personal');response=('Fikir' if kind=='idea' else 'Not')+' kişisel hafızanıza kaydedildi. Kayıt: '+did[:8]
                elif not attachment and text.startswith('/hatirlat '):
                    match=re.match(r'/hatirlat\s+(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?\s+(.+)',text,re.S)
                    if not match:raise Denied('reminder_format','Kullanım: /hatirlat 2026-10-05 14:30 hatırlatılacak şey',422)
                    due=match.group(1)+'T'+(match.group(2) or '09:00')+':00+03:00';content=match.group(3).strip()
                    archive.create_memory(c,u,content[:80],content,'reminder',{'status':'active','due_at':due},'personal');response='Hatırlatma kaydedildi: '+due[:16].replace('T',' ')
                elif not attachment and text.startswith('/jobs'):
                    items=c.execute("SELECT status,call_count FROM jobs WHERE user_id=%s AND status IN ('queued','running') ORDER BY created_at",(u['id'],)).fetchall();response='\n'.join(str(x['status'])+' · '+str(x['call_count'])+' çağrı' for x in items) or 'Devam eden işiniz yok.'
                elif not attachment and text.startswith('/new'):
                    c.execute('DELETE FROM telegram_conversations WHERE user_id=%s',(u['id'],));response='Yeni konuşma hazır. Mesajınızı yazın.'
                elif not attachment and text.startswith('/'):
                    response='Bu komutu tanımıyorum. /yardim ile tüm komutları görebilirsiniz.'
                elif text or attachment:
                    current=c.execute('SELECT conversation_id FROM telegram_conversations WHERE user_id=%s',(u['id'],)).fetchone()
                    pref=c.execute('SELECT model,scope FROM telegram_preferences WHERE user_id=%s',(u['id'],)).fetchone()
                    selected_scope=pref['scope'] if pref else 'personal'
                    document_ids=[]
                    if attachment:
                        from extraction import inspect
                        try:extracted,attachment['mime']=inspect(attachment['raw'],attachment['name'],attachment['mime'])
                        except Exception:raise Denied('file_unreadable','Dosya okunamadı veya bozuk. PDF, DOCX, XLSX, PPTX, metin ya da görsel olarak tekrar gönderebilirsiniz.',422)
                        storage_scope='board' if selected_scope=='board' else 'community' if selected_scope=='community' and u['role'] in BOARD else 'personal'
                        did=add_document(c,u,attachment['name'],extracted,scope=storage_scope,filedata=base64.b64encode(attachment['raw']).decode(),mime=attachment['mime'],hint=text)
                        if selected_scope=='community' and u['role'] not in BOARD:
                            c.execute("UPDATE documents SET review_status='submitted' WHERE id=%s",(did,))
                        document_ids=[did]
                        audit(c,u['id'],'telegram.attachment_received',did,{'name':attachment['name'],'mime':attachment['mime'],'bytes':len(attachment['raw']),'scope':storage_scope})
                        if not text.strip():text='Bu '+('fotoğrafı' if attachment['kind']=='photo' else 'dosyayı')+' incele, içeriğini özetle ve önemli noktaları belirt.'
                    response=direct_test_mail_preview(c,u,text,selected_scope) if not attachment else None
                    if not response:
                        result=enqueue(c,u,{'text':text,'model':pref['model'] if pref else MODELS[0],'scope':selected_scope,'conversation_id':str(current['conversation_id']) if current else None,'document_ids':document_ids},'telegram','telegram:'+str(data['update_id']))
                        source_time=int(m.get('date') or 0)
                        audit(c,u['id'],'telegram.received',str(result['id']),{'transport_delay_ms':max(0,int((time.time()-source_time)*1000)) if source_time else None})
                        c.execute('INSERT INTO telegram_conversations(user_id,conversation_id) VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE SET conversation_id=excluded.conversation_id',(u['id'],result['conversation_id']));response=None
                else:response='Mesaj yazabilir, fotoğraf veya dosya gönderebilirsiniz.'
    if response=='__CONSENT__':consent_prompt(tid)
    elif response:send(tid,response)
    elif text or m.get('document') or m.get('photo'):
        try:api('sendChatAction',{'chat_id':tid,'action':'typing'})
        except Exception:pass

def loop():
    try:
        api('setMyCommands',{'commands':[{'command':'menu','description':'Hızlı erişim menüsü'},{'command':'yardim','description':'Ayrıntılı kullanım rehberi'},{'command':'kisisel','description':'Kişisel alana geç'},{'command':'topluluk','description':'Topluluk alanına geç'},{'command':'yk','description':'YK özel alanı'},{'command':'alan','description':'Etkin alanı göster'},{'command':'arsiv','description':'Arşivde ara'},{'command':'fikir','description':'Yeni fikir kaydet'},{'command':'fikirler','description':'Fikirleri getir'},{'command':'not','description':'Kişisel not kaydet'},{'command':'hatirlat','description':'Zamanlı hatırlatma ekle'},{'command':'hatirlatmalar','description':'Hatırlatmaları getir'},{'command':'paylasimlar','description':'Paylaşımları göster'},{'command':'jobs','description':'Devam eden işleri göster'},{'command':'cancel','description':'Devam eden işi durdur'},{'command':'quota','description':'Kullanım durumunu göster'},{'command':'model','description':'Model seç'},{'command':'new','description':'Yeni konuşma'}]})
    except Exception:pass
    while True:
        try:
            with connect() as c:
                r=c.execute("SELECT value FROM settings WHERE key='telegram_offset'").fetchone();offset=r['value'] if r else 0
            updates=api('getUpdates',{'offset':offset,'timeout':20,'allowed_updates':['message','callback_query']})
            for data in updates:
                advance=True
                try:
                    update(data)
                    with connect() as c:c.execute('DELETE FROM telegram_update_failures WHERE update_id=%s',(data['update_id'],))
                except Denied as e:
                    actor=data.get('message',{}).get('from',{}).get('id') or data.get('callback_query',{}).get('from',{}).get('id')
                    if actor:send(actor,e.message)
                except Exception as e:
                    print('telegram update failed',type(e).__name__,flush=True)
                    from incident_center import record
                    record('telegram','telegram_update_failed','Telegram mesajı işlenemedi.',{'exception':type(e).__name__})
                    with connect() as c:
                        failure=c.execute("""INSERT INTO telegram_update_failures(update_id,last_error) VALUES(%s,%s)
                          ON CONFLICT(update_id) DO UPDATE SET attempts=telegram_update_failures.attempts+1,last_error=excluded.last_error,updated_at=now()
                          RETURNING attempts""",(data['update_id'],type(e).__name__)).fetchone()
                    advance=failure['attempts']>=3
                    if not advance:break
                if advance:
                    with connect() as c:c.execute("INSERT INTO settings VALUES('telegram_offset',%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(dumps(data['update_id']+1),))
        except Exception as e:
            print('telegram unavailable',type(e).__name__,flush=True)
            from incident_center import record
            record('telegram','telegram_unavailable','Telegram bağlantısı kurulamadı.',{'exception':type(e).__name__},'critical');time.sleep(10)

def deliver():
    with connect() as c:
        row=c.execute("SELECT n.*,u.telegram_id,u.status AS account_status FROM notifications n JOIN platform_users u ON u.id=n.user_id WHERE n.status='pending' AND n.next_attempt_at<=now() ORDER BY n.id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not row:return
        c.execute("UPDATE notifications SET status='sending' WHERE id=%s",(row['id'],))
    status='skipped'
    if row['telegram_id'] and row['account_status']=='active':
        try:
            payload=decrypt(row['body']);text=payload.get('text','') if isinstance(payload,dict) else str(payload);attachments=payload.get('attachments',[]) if isinstance(payload,dict) else []
            if text:send(row['telegram_id'],text)
            for item in attachments:
                if not isinstance(item,dict) or not item.get('id'):continue
                with connect() as c:document=c.execute('SELECT * FROM documents WHERE id=%s AND owner_id=%s',(item['id'],row['user_id'])).fetchone()
                if not document:continue
                content=decrypt(document['body']);raw=base64.b64decode(content['file']) if content.get('file') else content.get('text','').encode()
                send_attachment(row['telegram_id'],document['name'],raw,document['mime'])
            status='sent'
        except Exception as exc:
            status='uncertain' if row['attempts']>=4 else 'pending'
            from incident_center import record
            record('telegram','telegram_delivery_failed','Telegram yanıtı teslim edilemedi.',{'exception':type(exc).__name__},'error',fingerprint_key=str(row['user_id']))
            with connect() as c:
                c.execute("UPDATE notifications SET status=%s,attempts=attempts+1,last_error=%s,next_attempt_at=now()+(interval '5 seconds' * power(2,least(attempts,5))) WHERE id=%s",(status,type(exc).__name__,row['id']))
            return
    with connect() as c:c.execute('UPDATE notifications SET status=%s,body=%s,last_error=NULL WHERE id=%s',(status,encrypt(''),row['id']))

def typing_tick():
    with connect() as c:
        chats=c.execute("SELECT DISTINCT u.telegram_id FROM jobs j JOIN platform_users u ON u.id=j.user_id WHERE j.channel='telegram' AND j.status IN ('queued','running') AND NOT j.cancel_requested AND u.status='active' AND u.telegram_id IS NOT NULL").fetchall()
    for row in chats:
        try:api('sendChatAction',{'chat_id':row['telegram_id'],'action':'typing'})
        except Exception:pass

def typing_loop():
    """Refresh Telegram's short-lived typing indicator while a job is active."""
    while True:
        try:typing_tick()
        except Exception as exc:print('telegram typing unavailable',type(exc).__name__,flush=True)
        time.sleep(4)

def delivery_loop():
    while True:
        try:deliver()
        except Exception as exc:print('telegram delivery unavailable',type(exc).__name__,flush=True)
        time.sleep(.2)

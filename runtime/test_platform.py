import os,uuid
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
import pytest
from common import *

def test_worker_retry_rotates_model_once():
    import runner
    jid,_=job()
    with connect() as c:
        row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    assert runner.maybe_retry(row,'worker_error') is True
    with connect() as c:
        updated=c.execute('SELECT status,model FROM jobs WHERE id=%s',(jid,)).fetchone()
    assert updated['status']=='queued' and updated['model']==runner.next_model(row['model'])
    assert runner.worker_session_id({**row,'id':jid})!=str(row['conversation_id'])
    assert runner.maybe_retry(row,'worker_error') is False

def test_pending_approval_survives_worker_failure():
    import runner
    jid,_=job(uid=4);aid=str(uuid.uuid4())
    with connect() as c:
        c.execute("INSERT INTO action_approvals(id,user_id,job_id,kind,preview,payload,expires_at) VALUES(%s,%s,%s,'gmail_send',%s,%s,now()+interval '15 minutes')",(aid,4,jid,encrypt({'to':['recipient@test.invalid']}),encrypt({})))
        row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    message=runner.pending_approval_completion(row)
    assert message.startswith('E-posta önizlemesi hazır.')
    with connect() as c:
        approval=c.execute('SELECT status FROM action_approvals WHERE id=%s',(aid,)).fetchone()
        event=c.execute("SELECT detail FROM audit_events WHERE action='job.pending_approval_recovered' AND target=%s",(jid,)).fetchone()
    assert approval['status']=='pending'
    assert event['detail']['approval_id']==aid

def test_worker_container_start_retries_docker_name_release(monkeypatch):
    import runner
    calls=[];starts=0
    class Result:
        def __init__(self,code,stderr=''):self.returncode=code;self.stderr=stderr;self.stdout=''
    def fake_run(args,**kwargs):
        nonlocal starts
        calls.append(args)
        if args[:3]==['docker','rm','-f']:return Result(0)
        starts+=1
        return Result(125,'Conflict. The container name is already in use') if starts==1 else Result(0)
    monkeypatch.setattr(runner.subprocess,'run',fake_run)
    monkeypatch.setattr(runner.time,'sleep',lambda _:None)
    runner.start_worker_container(['docker','run','image'],'inovens-user-4')
    assert starts==2 and any(x[:3]==['docker','rm','-f'] for x in calls)

def test_incident_center_is_owner_only_and_deduplicates():
    from incident_center import record
    first=record('test','synthetic_error','Sentetik hata',{'token':'must-not-be-stored','service':'general_router'})
    second=record('test','synthetic_error','Sentetik hata tekrarlandı',{'service':'general_router'})
    assert first==second
    data=dispatch(1,'incidents','GET',{})
    item=next(x for x in data['items'] if str(x['id'])==first)
    assert item['occurrences']==2 and item['status']=='open' and 'must-not-be-stored' not in dumps(item['detail'])
    with pytest.raises(Denied):dispatch(4,'incidents','GET',{})
    started=dispatch(1,'incidents/'+first+'/fix','POST',{})
    duplicate=dispatch(1,'incidents/'+first+'/fix','POST',{})
    assert duplicate['repair_id']==started['repair_id'] and duplicate['already_running'] is True

def test_project_owner_actions_reject_board_roles():
    protected=[
        ('users','GET',{}),
        ('settings','GET',{}),
        ('invitations','GET',{}),
        ('audit','GET',{}),
        ('system','GET',{}),
        ('system/actions','POST',{'action':'pause'}),
    ]
    for path,method,body in protected:
        with pytest.raises(Denied) as denied:
            dispatch(4,path,method,body)
        assert denied.value.code=='owner_required'
    assert 'items' in dispatch(1,'users','GET',{})

def test_inovens_github_is_owner_only_on_web_and_telegram(monkeypatch):
    import github_broker,tool_broker
    calls=[]
    monkeypatch.setattr(github_broker,'connected_account',lambda:'inovensai')
    monkeypatch.setattr(github_broker,'run_gh',lambda args:calls.append(args) or '[]')
    for uid,channel in [(2,'web'),(4,'telegram')]:
        jid,_=job(uid=uid)
        with connect() as c:
            c.execute('UPDATE jobs SET channel=%s WHERE id=%s',(channel,jid))
            row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
        with pytest.raises(Denied) as denied:
            tool_broker.call({'id':uid},row,'github_read',{'action':'list_repos'})
        assert denied.value.code=='owner_required'
    for channel in ['web','telegram']:
        jid,_=job(uid=1)
        with connect() as c:
            c.execute('UPDATE jobs SET channel=%s WHERE id=%s',(channel,jid))
            row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
        result=tool_broker.call({'id':1},row,'github_read',{'action':'list_repos'})
        assert result['result']=='[]'
    assert len(calls)==2 and all(args[0:3]==['repo','list','inovensai'] for args in calls)

def test_github_broker_rejects_wrong_account_and_invalid_repo(monkeypatch):
    import github_broker
    monkeypatch.setattr(github_broker,'connected_account',lambda:'ege-arhan')
    with pytest.raises(Denied) as denied:
        github_broker.call('github_write',{'action':'create_issue','repo':'test','title':'Synthetic','body':'Synthetic'})
    assert denied.value.code=='github_not_connected'
    monkeypatch.setattr(github_broker,'connected_account',lambda:'inovensai')
    with pytest.raises(Denied) as denied:
        github_broker.call('github_read',{'action':'repo','repo':'other/repo'})
    assert denied.value.code=='github_repo_invalid'

def test_github_broker_creates_private_repo_only_under_inovens_account(monkeypatch):
    import github_broker
    calls=[]
    monkeypatch.setattr(github_broker,'connected_account',lambda:'inovensai')
    monkeypatch.setattr(github_broker,'run_gh',lambda args:calls.append(args) or 'https://github.com/inovensai/deneme')
    result=github_broker.call('github_write',{'action':'create_repo','repo':'deneme','description':'Synthetic'})
    assert result['result'].endswith('/deneme')
    assert calls==[['repo','create','inovensai/deneme','--private','--add-readme','--description','Synthetic']]

def test_board_can_manage_community_work_but_not_owner_scoped_work():
    created=dispatch(4,'operations','POST',{'title':'YK toplantısını hazırla','scope':'board'})
    assert any(x['title']=='YK toplantısını hazırla' for x in created['items'])
    with pytest.raises(Denied) as denied:
        dispatch(4,'operations','POST',{'title':'Yalnız sahibi görsün','scope':'owner'})
    assert denied.value.code=='owner_required'
    owner_item=dispatch(1,'operations','POST',{'title':'Sahip notu','scope':'owner'})
    item=next(x for x in owner_item['items'] if x['title']=='Sahip notu')
    with pytest.raises(Denied) as denied:
        dispatch(4,'operations/'+str(item['id']),'PATCH',{'title':'Değiştir'})
    assert denied.value.code=='owner_required'
    assert all(x['id']!=item['id'] for x in dispatch(4,'operations','GET',{})['items'])

def test_late_success_reconciles_fixer_incident():
    from incident_center import record,reconcile_completed_jobs
    jid,_=job();iid=record('jobs','worker_error','Synthetic failed job',{},related_job=jid)
    rid=str(uuid.uuid4())
    with connect() as c:
        c.execute("UPDATE jobs SET status='done',error_code=NULL WHERE id=%s",(jid,))
        c.execute("INSERT INTO repair_runs(id,incident_id,status,evidence) VALUES(%s,%s,'needs_review',%s)",(rid,iid,dumps({'job_check':{'status':'still_running'}})))
    reconcile_completed_jobs()
    with connect() as c:
        incident=c.execute('SELECT status FROM incidents WHERE id=%s',(iid,)).fetchone()
        repair=c.execute('SELECT status,evidence FROM repair_runs WHERE id=%s',(rid,)).fetchone()
    assert incident['status']=='resolved' and repair['status']=='fixed'
    assert repair['evidence']['late_job_check']['status']=='done'

def test_empty_worker_response_gets_three_isolated_retries():
    import runner
    jid,_=job()
    sessions=[]
    for attempt in range(4):
        with connect() as c:row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
        sessions.append(runner.worker_session_id(row))
        retried=runner.maybe_retry(row,'empty_worker_response')
        assert retried is (attempt<3)
    assert len(set(sessions))==4
from quota import *
from portal import dispatch,add_document,search_documents,can_document,enqueue,normalize_request_text

@pytest.fixture(autouse=True)
def clean(monkeypatch,tmp_path):
    import tool_broker
    monkeypatch.setattr(tool_broker,"ROOT",tmp_path)
    assert os.environ['DATABASE_URL'].endswith('/inovens_test'), 'Never run destructive fixtures on production'
    with connect() as c:
        c.execute('TRUNCATE platform_users CASCADE');c.execute('TRUNCATE audit_events,settings,invitations CASCADE')
        c.execute("INSERT INTO settings VALUES('limits','{\"daily_try\":30,\"monthly_try\":1000,\"usd_try\":50,\"pilot_size\":10,\"enabled\":true}')")
        import archive
        for uid,role in [(1,'owner'),(2,'member'),(3,'member'),(4,'board')]:c.execute('INSERT INTO platform_users(id,email,name,role,status,consent_version,telegram_consent_version) VALUES(%s,%s,%s,%s,%s,%s,%s)',(uid,str(uid)+'@test.invalid','Test '+str(uid),role,'active',CONSENT,archive.TG_CONSENT))

def job(uid=2):
    jid=str(uuid.uuid4());cid=str(uuid.uuid4())
    with connect() as c:
        c.execute('INSERT INTO conversations(id,user_id,title) VALUES(%s,%s,%s)',(cid,uid,encrypt('private')))
        c.execute('INSERT INTO jobs(id,user_id,conversation_id,channel,status,model,prompt,request_key) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(jid,uid,cid,'web','running',MODELS[0],encrypt({}),jid))
    return jid,cid
PRICE={'input':.1,'output':.2,'cached':.002}
def test_midnight():
    before=datetime(2026,9,6,20,59,59,tzinfo=timezone.utc);after=datetime(2026,9,6,21,0,0,tzinfo=timezone.utc)
    assert str(day_key(before))=='2026-09-06';assert str(day_key(after))=='2026-09-07';assert next_reset(before).hour==0

def test_pdf_mobile_typo_is_normalized_only_for_model_prompt():
    assert normalize_request_text('7 eylüldeki haberleri pf şeklinde ilet')=='7 eylüldeki haberleri PDF şeklinde ilet'
    assert normalize_request_text('pf hesabı nedir?')=='pf hesabı nedir?'
    with connect() as c:
        result=enqueue(c,user(c,2),{'text':'Haberleri pf şeklinde ilet'})
        jobrow=c.execute('SELECT prompt,conversation_id FROM jobs WHERE id=%s',(result['id'],)).fetchone()
        stored=c.execute("SELECT body FROM messages WHERE conversation_id=%s AND role='user'",(jobrow['conversation_id'],)).fetchone()
    assert decrypt(jobrow['prompt'])['text']=='Haberleri PDF şeklinde ilet'
    assert decrypt(stored['body'])=='Haberleri pf şeklinde ilet'

def test_execution_contract_requires_sources_and_real_pdf_for_dated_news():
    from execution_contract import infer
    contract=infer("7 Eylül 2026 Türkiye spor haberlerini PDF olarak ver")
    assert contract['required_extensions']==['.pdf']
    assert contract['source_required'] is True and contract['discovery_required'] is True
    assert contract['explicit_date'] is True
    assert contract['min_source_reads']==3
    assert contract['required_tools'][-1]['any_of']==['document_create']

def test_email_drafting_does_not_research_words_inside_the_template():
    from execution_contract import infer
    contract=infer('WhatsApp üzerinden duyurular yapılacağını belirten bir e-posta taslağı oluştur')
    assert contract['source_required'] is False
    assert contract['discovery_required'] is False
    assert infer('MD dosyasında kaynakça belirtme')['source_required'] is False

def test_fast_reply_keeps_only_public_conversation_context():
    import quick_reply
    jid,cid=job(uid=4)
    with connect() as c:
        c.execute('INSERT INTO messages(conversation_id,role,body) VALUES(%s,%s,%s)',(cid,'user',encrypt('Galatasaray bugün kiminle oynuyor?')))
        c.execute('INSERT INTO messages(conversation_id,role,body) VALUES(%s,%s,%s)',(cid,'assistant',encrypt('Galatasaray Trabzonspor ile oynuyor.')))
        c.execute('INSERT INTO messages(conversation_id,role,body) VALUES(%s,%s,%s)',(cid,'user',encrypt('API anahtarım secret-value')))
        c.execute('INSERT INTO messages(conversation_id,role,body) VALUES(%s,%s,%s)',(cid,'user',encrypt('Maç kaç kaç?')))
        row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    context=quick_reply.conversation_context(row,'Maç kaç kaç?')
    combined=' '.join(x['content'] for x in context)
    assert 'Trabzonspor' in combined and 'secret-value' not in combined

def test_execution_contract_extracts_requested_count_not_year():
    from execution_contract import infer
    contract=infer('2026 Çin AI/ML alanında 30 patent ve makaleyi araştırıp PDF yap')
    assert contract['expected_item_count']==30
    assert contract['min_source_reads']==8
    assert [x['any_of'] for x in contract['required_tools']]==[['document_create']]

def test_execution_contract_does_not_browse_to_format_supplied_text():
    from execution_contract import infer
    contract=infer('Bu metni düzenleyip PDF yap')
    assert contract['required_extensions']==['.pdf']
    assert contract['source_required'] is False

def test_execution_contract_uses_latest_sources_when_date_is_omitted():
    from execution_contract import infer
    contract=infer('Türkiye spor haberlerini PDF olarak hazırla')
    assert contract['source_required'] is True and contract['explicit_date'] is False
    assert contract['required_extensions']==['.pdf']

def test_image_generation_contract_is_limited_to_community_scope():
    from execution_contract import infer
    board_contract=infer("INOVENS için 16:9 bir etkinlik afişi oluştur",'board')
    assert board_contract['image_requested'] is True
    assert board_contract['required_extensions']==['.png']
    assert [item['any_of'][0] for item in board_contract['required_tools']]==['image_create']
    personal_contract=infer("INOVENS için bir etkinlik afişi oluştur",'personal')
    assert personal_contract['image_requested'] is True
    assert personal_contract['required_extensions']==[] and personal_contract['required_tools']==[]

def test_explicit_image_format_is_part_of_contract():
    from execution_contract import infer
    contract=infer('Topluluk için kare bir logo görseli JPEG olarak oluştur','board')
    assert contract['required_extensions']==['.jpg']
    assert contract['required_tools'][0]['any_of']==['image_create']

@pytest.mark.parametrize(('text','tools'),[
    ('topluluk mailinden başkana gönder',['gmail_send']),
    ('maillerimde etkinlik başvurusunu ara',['gmail_search']),
    ("Drive'daki tüzüğü indir",['drive_search','drive_download']),
    ("bu dosyayı Drive'a yükle",['drive_upload']),
    ('takvimde yaklaşan etkinlikleri göster',['calendar_upcoming']),
    ('arşivde geçmiş kararları bul',['knowledge_search']),
])
def test_execution_contract_maps_external_actions_to_tools(text,tools):
    from execution_contract import infer
    assert [item['any_of'][0] for item in infer(text)['required_tools']]==tools

def test_execution_contract_requires_github_tools_for_owner_requests():
    from execution_contract import infer
    assert [item['any_of'][0] for item in infer('GitHubda hangi repolar var')['required_tools']]==['github_read']
    assert [item['any_of'][0] for item in infer('GitHubda deneme repo aç')['required_tools']]==['github_write']

def test_execution_contract_rejects_claimed_but_missing_pdf(tmp_path):
    from execution_contract import completion_issue,infer
    jid,_=job()
    with connect() as c:
        c.execute('UPDATE jobs SET prompt=%s WHERE id=%s',(encrypt({'text':'Bu metni PDF yap','execution_contract':infer('Bu metni PDF yap')}),jid))
        audit(c,2,'tool.used',jid,{'tool':'document_create'})
        row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    home=tmp_path/'worker';(home/'workspace').mkdir(parents=True)
    issue=completion_issue(row,home,'Hazır.\nMEDIA:/workspace/olmayan.pdf',[],lambda text,structured:['/workspace/olmayan.pdf'])
    assert issue=='deliverable_missing'

def test_execution_contract_requires_search_and_source_read_for_news(tmp_path):
    from execution_contract import completion_issue,infer
    jid,_=job()
    with connect() as c:
        c.execute('UPDATE jobs SET prompt=%s WHERE id=%s',(encrypt({'text':'Bugünkü haberleri ver','execution_contract':infer('Bugünkü haberleri ver')}),jid))
        row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    assert completion_issue(row,tmp_path,'Yanıt',[],lambda *_:[])=='source_search_missing'
    with connect() as c:audit(c,2,'tool.used',jid,{'tool':'web_search'})
    assert completion_issue(row,tmp_path,'Yanıt',[],lambda *_:[])=='source_read_missing'
    with connect() as c:
        for i in range(3):audit(c,2,'tool.used',jid,{'tool':'web_fetch','source_url':f'https://example.com/{i}'})
    assert completion_issue(row,tmp_path,'Yanıt',[],lambda *_:[]) is None

def test_document_factory_creates_verified_turkish_pdf(monkeypatch,tmp_path):
    import document_factory
    monkeypatch.setattr(document_factory,'ROOT',tmp_path)
    result=document_factory.create({'id':2,'workspace_epoch':0},{},{'filename':'güncel-spor.pdf','format':'pdf','title':'Güncel Spor Haberleri','content':'# Özet\nTürkiye spor gündeminden doğrulanmış gelişmeler.\n\n- Birinci gelişme\n- İkinci gelişme','sources':['https://example.com/news']})
    path=tmp_path/'users/2/personal-worker/0/workspace/güncel-spor.pdf'
    assert result['path']=='/workspace/güncel-spor.pdf' and result['verified'] is True
    assert path.is_file() and path.stat().st_size>1000

def test_upload_inspection_rejects_extension_spoofing_and_zip_bombs():
    import io,zipfile
    from extraction import inspect
    with pytest.raises(ValueError):inspect(b'not a pdf','report.pdf','application/pdf')
    with pytest.raises(ValueError):inspect(b'\x89PNG\r\n\x1a\nnot-really-an-image','photo.png','image/png')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','x')
        archive.writestr('word/document.xml','A'*6_000_000)
    with pytest.raises(ValueError):inspect(buffer.getvalue(),'report.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document')

def test_upload_inspection_uses_verified_mime_instead_of_browser_claim():
    from extraction import inspect
    text,mime=inspect(b'hello\nworld','notes.txt','application/x-executable')
    assert text=='hello\nworld' and mime=='text/plain'

def test_incident_monitor_does_not_treat_timeout_configuration_as_error():
    from incident_center import JOURNAL_ERRORS
    assert not JOURNAL_ERRORS.search('model-fetch timeoutMs=undefined status=200')
    assert JOURNAL_ERRORS.search('provider request timed out')
    assert JOURNAL_ERRORS.search('HTTP 503 from upstream')

def test_sync_status_exposes_progress_without_api_key_or_filenames(monkeypatch,tmp_path):
    import io,system_status
    config=tmp_path/'.config/syncthing/config.xml';config.parent.mkdir(parents=True)
    config.write_text('<configuration><gui><address>127.0.0.1:8384</address><apikey>synthetic-key</apikey></gui></configuration>')
    monkeypatch.setattr(system_status.Path,'home',lambda:tmp_path)
    def respond(request,timeout):
        assert request.headers['X-api-key']=='synthetic-key'
        return io.BytesIO(b'{"state":"idle","needFiles":0,"needBytes":0,"errors":0,"fileName":"private.txt"}')
    monkeypatch.setattr(system_status.urllib.request,'urlopen',respond)
    result=system_status.sync_status()
    assert result['available'] and len(result['folders'])==2
    assert 'synthetic-key' not in dumps(result) and 'private.txt' not in dumps(result)

def test_document_factory_creates_polished_long_report(monkeypatch,tmp_path):
    import fitz,document_factory
    monkeypatch.setattr(document_factory,'ROOT',tmp_path)
    entries=[]
    for index in range(1,11):
        entries.append(f'## {index}. Doğrulanmış gelişme {index}\nKurum {index}, 2026 döneminde yeni bir çalışma yayımladı. Çalışmanın yöntemi, uygulama alanı ve Türkiye açısından etkisi kaynak üzerinden incelendi. Bu kayıt karar vericiler için ölçülebilir bir fırsat ve uygulanabilir bir takip alanı sunuyor. Bulgular kesin iddia yerine kaynağın açıkladığı kapsamla sınırlandırıldı.')
    content='# Yönetici Özeti\nBu rapor doğrulanmış gelişmeleri karşılaştırır.\n\n# Yöntem ve Kapsam\nKaynaklar ayrı ayrı açıldı ve bulgular çapraz değerlendirildi.\n\n# Bulgular\n'+('\n\n'.join(entries))+'\n\n# Sonuç\nÖncelikler etki ve uygulanabilirliğe göre sıralandı.'
    sources=[f'https://example.com/research/{i}' for i in range(1,11)]
    result=document_factory.create({'id':2,'workspace_epoch':0},{},{'filename':'kaliteli-rapor.pdf','format':'pdf','title':'2026 Araştırma Raporu','content':content,'sources':sources})
    path=tmp_path/'users/2/personal-worker/0/workspace/kaliteli-rapor.pdf';doc=fitz.open(path)
    assert result['quality_check']=='passed' and result['pages']>=4
    assert doc.metadata['title']=='2026 Araştırma Raporu' and doc.metadata['author']=="INOVENS'AI"
    assert any('DejaVu' in font[3] for page in doc for font in page.get_fonts(full=True))
    assert len([link for page in doc for link in page.get_links()])>=10
    doc.close()

def test_document_factory_removes_duplicate_source_appendix_and_finishes_summary():
    from document_factory import without_source_appendix,plain_summary
    content='# Yönetici Özeti\nİlk doğrulanmış cümle burada biter. İkinci doğrulanmış cümle de burada tamamlanır.\n\n# Bulgular\n1. Kayıt\nAçıklama.\n\nKaynakça (erişim: Eylül 2026):\nhttps://example.com/a'
    rows=without_source_appendix(content,['https://example.com/a'])
    assert not any('Kaynakça' in row or 'https://' in row for row in rows)
    summary=plain_summary(content,55)
    assert summary.endswith('.') and not summary.endswith('…')

def test_image_factory_creates_and_visually_verifies_output(monkeypatch,tmp_path):
    import json,subprocess
    from PIL import Image,ImageDraw
    import image_factory
    executable=tmp_path/'openclaw';executable.write_text('test')
    monkeypatch.setattr(image_factory,'ROOT',tmp_path)
    monkeypatch.setattr(image_factory,'OPENCLAW',executable)
    calls=[]
    def generated(command,**kwargs):
        calls.append((command,kwargs));path=Path(command[command.index('--output')+1])
        image=Image.new('RGB',(1024,1024),'white');draw=ImageDraw.Draw(image);draw.rectangle((80,80,944,944),fill='#173d2a');draw.ellipse((260,260,764,764),fill='#ddf3e6');image.save(path)
        payload={'ok':True,'outputs':[{'path':str(path),'mimeType':'image/png','size':path.stat().st_size,'width':1024,'height':1024}]}
        return subprocess.CompletedProcess(command,0,stdout=json.dumps(payload),stderr='')
    monkeypatch.setattr(image_factory.subprocess,'run',generated)
    result=image_factory.create({'id':4,'role':'board','workspace_epoch':0},{'scope':'board'},{'prompt':'Yeşil geometrik bir INOVENS etkinlik görseli','filename':'etkinlik.png','quality':'medium'})
    path=tmp_path/'users/4/personal-worker/0/workspace/etkinlik.png'
    assert result['path']=='/workspace/etkinlik.png' and result['quality_check']=='passed'
    assert result['width']==1024 and result['height']==1024 and path.is_file()
    assert isinstance(calls[0][0],list) and '--agent' in calls[0][0] and calls[0][0][calls[0][0].index('--agent')+1]=='ai-ege'

def test_image_tool_rejects_personal_scope_and_allows_board(monkeypatch):
    import image_factory,tool_broker
    personal_job,_=job(4)
    with connect() as c:u=user(c,4);row=c.execute('SELECT * FROM jobs WHERE id=%s',(personal_job,)).fetchone()
    with pytest.raises(Denied) as error:tool_broker.call(u,row,'image_create',{'prompt':'Topluluk için yeşil bir poster tasarla'})
    assert error.value.code=='image_community_only'
    board_job,conversation_id=job(4)
    with connect() as c:
        c.execute("UPDATE conversations SET scope='board' WHERE id=%s",(conversation_id,))
        u=user(c,4);row=c.execute('SELECT * FROM jobs WHERE id=%s',(board_job,)).fetchone()
    monkeypatch.setattr(image_factory,'create',lambda user_value,job_value,args:{'path':'/workspace/test.png','filename':'test.png','format':'png','verified':True})
    result=tool_broker.call(u,row,'image_create',{'prompt':'Topluluk için yeşil bir poster tasarla'})
    assert result['path']=='/workspace/test.png'

def test_reservation_and_idempotent_settle():
    j,_=job();cid=reserve(2,j,'model','test',1000,100,PRICE);settle(cid,{'input':100,'output':10,'cached':50,'reasoning':2});settle(cid)
    with connect() as c:q=usage_view(c,2)
    assert q['used']==110 and q['reserved']==0

def test_concurrency_cannot_overspend():
    j,_=job()
    with connect() as c:c.execute('UPDATE platform_users SET daily_limit=1000 WHERE id=2')
    def attempt(_):
        try:return reserve(2,j,'model','test',500,100,PRICE)
        except Denied:return None
    with ThreadPoolExecutor(max_workers=8) as p:results=list(p.map(attempt,range(8)))
    assert sum(x is not None for x in results)==1

def test_cross_user_reservation_denied():
    j,_=job()
    with pytest.raises(Denied):reserve(3,j,'m','t',1,1,PRICE)

def test_global_budget():
    j,_=job()
    with connect() as c:c.execute("UPDATE settings SET value=jsonb_set(value,'{daily_try}','0.000001') WHERE key='limits'")
    with pytest.raises(Denied) as e:reserve(2,j,'m','t',100,10,PRICE)
    assert e.value.code=='daily_budget_limit'

def test_unknown_charged_and_unbilled_refunded():
    j,_=job();a=reserve(2,j,'m','t',100,10,PRICE);settle(a,error='timeout');b=reserve(2,j,'m','t',100,10,PRICE);settle(b,definitely_unbilled=True)
    with connect() as c:q=usage_view(c,2)
    assert q['used']==110 and q['reserved']==0

def test_deletion_keeps_accounting():
    j,cid=job();a=reserve(2,j,'m','t',100,10,PRICE);dispatch(2,'conversations/'+cid,'DELETE',{});settle(a)
    with connect() as c:
        assert c.execute('SELECT conversation_id FROM jobs WHERE id=%s',(j,)).fetchone()['conversation_id'] is None
        assert usage_view(c,2)['used']==110

def test_personal_conversation_hidden():
    _,cid=job()
    with pytest.raises(Denied):dispatch(3,'conversations/'+cid,'GET',{})
    with pytest.raises(Denied):dispatch(1,'conversations/'+cid,'GET',{})

def test_member_google_and_admin_denied():
    for path,method,body in [('users','GET',{}),('settings','PATCH',{'enabled':True}),('chat','POST',{'text':'read email','scope':'google'}),('approvals','GET',{})]:
        with pytest.raises(Denied):dispatch(2,path,method,body)

def test_document_acl_before_search():
    with connect() as c:
        did=add_document(c,user(c,2),'Private','secretpineapple information')
        assert not search_documents(c,user(c,3),'secretpineapple')
        assert not search_documents(c,user(c,1),'secretpineapple')
        assert len(search_documents(c,user(c,2),'secretpineapple'))==1

def test_personal_document_share_requires_recipient_acceptance():
    import archive
    with connect() as c:
        did=add_document(c,user(c,2),'Signals final','private-almond',kind='exam',metadata={'academic_year':'2025-2026','course':'Signals','exam_type':'final'})
        invitation=archive.create_share(c,user(c,2),did,'3','Ders çalışırken bakabilirsin.')
        assert not search_documents(c,user(c,3),'private-almond')
        archive.share_action(c,user(c,3),invitation['id'],'accept')
        assert search_documents(c,user(c,3),'private-almond')
        archive.send_message(c,user(c,3),invitation['id'],'Teşekkürler.')
        assert archive.list_messages(c,user(c,2),invitation['id'])['items'][0]['body']=='Teşekkürler.'

def test_archive_metadata_and_memory_are_scoped():
    import archive
    with connect() as c:
        kind,meta,review=archive.classify_document('EEM 2. sınıf 2. dönem 2025-2026 x dersi vize.pdf')
        assert kind=='exam' and meta['academic_year']=='2025-2026' and meta['grade']==2 and meta['semester']==2 and meta['course']=='x' and meta['exam_type']=='vize'
        did=archive.create_memory(c,user(c,2),'Proje fikri','Kampüs çalışma uygulaması','idea',{'status':'active'},'personal')
        assert str(archive.list_documents(c,user(c,2),'Proje fikri',{'kind':'idea'})['items'][0]['id'])==did
        assert not archive.list_documents(c,user(c,3),'Proje fikri',{'kind':'idea'})['items']

def test_archive_update_classifies_only_owned_uploads():
    import tool_broker
    jid,_=job(2)
    with connect() as c:
        own=add_document(c,user(c,2),'scan.pdf','Elektrik makineleri ara sınav soruları')
        foreign=add_document(c,user(c,3),'other.pdf','Başka kullanıcının belgesi')
        u=user(c,2);row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    result=tool_broker.call(u,row,'archive_update',{'document_id':own,'kind':'exam','academic_year':'2025-2026','grade':2,'semester':2,'course':'Elektrik Makineleri','exam_type':'vize'})
    assert result['kind']=='exam' and result['metadata']['course']=='Elektrik Makineleri'
    with connect() as c:
        stored=c.execute('SELECT archive_kind,archive_meta,review_status FROM documents WHERE id=%s',(own,)).fetchone()
    assert stored['archive_kind']=='exam' and stored['archive_meta']['semester']==2 and stored['review_status']=='ready'
    with pytest.raises(Denied) as denied:
        tool_broker.call(u,row,'archive_update',{'document_id':foreign,'kind':'course'})
    assert denied.value.code=='document_owner'

def test_member_submission_needs_board_review():
    with connect() as c:
        did=add_document(c,user(c,2),'EEM vize','synthetic exam content',kind='exam')
        c.execute("UPDATE documents SET review_status='submitted' WHERE id=%s",(did,))
    assert any(str(x['id'])==did for x in dispatch(4,'archive','GET',{})['items'])
    dispatch(4,'archive/review/'+did,'POST',{'decision':'approve'})
    assert any(str(x['id'])==did and x['scope']=='community' for x in dispatch(3,'archive','GET',{})['items'])

def test_role_revoke_stops_job():
    j,_=job();dispatch(1,'users/2','PATCH',{'status':'revoked'})
    with pytest.raises(Denied):reserve(2,j,'m','t',1,1,PRICE)

def test_contributor_never_gets_google_search():
    with connect() as c:
        add_document(c,user(c,1),'Google doc','restrictedmango',google=True)
        assert not search_documents(c,user(c,1),'restrictedmango',False)
        assert search_documents(c,user(c,1),'restrictedmango',True)

def test_telegram_jobs_are_scheduled_first(monkeypatch):
    import runner
    web,_=job(2);telegram,_=job(3)
    with connect() as c:
        c.execute("UPDATE jobs SET status='queued',channel='web',created_at=now()-interval '1 minute' WHERE id=%s",(web,))
        c.execute("UPDATE jobs SET status='queued',channel='telegram' WHERE id=%s",(telegram,))
    selected=[]
    monkeypatch.setattr(runner.POOL,'submit',lambda fn,item:selected.append(item))
    runner.tick()
    assert [str(item['id']) for item in selected][:2]==[telegram,web]

def test_telegram_typing_for_active_job(monkeypatch):
    import telegram_bot
    jid,_=job(2)
    with connect() as c:
        c.execute("UPDATE platform_users SET telegram_id=12345 WHERE id=2")
        c.execute("UPDATE jobs SET channel='telegram' WHERE id=%s",(jid,))
    sent=[]
    monkeypatch.setattr(telegram_bot,'api',lambda method,body:sent.append((method,body)))
    telegram_bot.typing_tick()
    assert sent==[('sendChatAction',{'chat_id':12345,'action':'typing'})]

def test_telegram_photo_upload_is_attached_to_private_conversation(monkeypatch):
    import io
    from PIL import Image
    import telegram_bot
    image=Image.new('RGB',(40,30),'green');buffer=io.BytesIO();image.save(buffer,format='JPEG')
    with connect() as c:c.execute('UPDATE platform_users SET telegram_id=2222 WHERE id=2')
    monkeypatch.setattr(telegram_bot,'incoming_attachment',lambda message:{'name':'telegram-fotograf-test.jpg','mime':'image/jpeg','raw':buffer.getvalue(),'kind':'photo'})
    actions=[];monkeypatch.setattr(telegram_bot,'api',lambda method,body:actions.append((method,body)) or {})
    telegram_bot.update({'update_id':991,'message':{'from':{'id':2222},'chat':{'type':'private'},'date':int(datetime.now(timezone.utc).timestamp()),'photo':[{'file_id':'photo-id'}]}})
    with connect() as c:
        document=c.execute('SELECT id,name,scope,mime FROM documents WHERE owner_id=2').fetchone()
        queued=c.execute("SELECT conversation_id,prompt FROM jobs WHERE user_id=2 AND channel='telegram'").fetchone()
        message=c.execute("SELECT body FROM messages WHERE conversation_id=%s AND role='user'",(queued['conversation_id'],)).fetchone()
    prompt=decrypt(queued['prompt']);stored=decrypt(message['body'])
    assert document['name']=='telegram-fotograf-test.jpg' and document['scope']=='personal'
    assert prompt['documents'][0]['source_id']==str(document['id']) and len(prompt['images'])==1
    assert stored['attachments']==[{'id':str(document['id']),'name':'telegram-fotograf-test.jpg','mime':'image/jpeg'}]
    assert prompt['text'].startswith('Bu fotoğrafı incele')
    assert actions and actions[-1][0]=='sendChatAction'

def test_followup_question_reuses_latest_conversation_attachment():
    with connect() as c:
        u=user(c,2);did=add_document(c,u,'notlar.txt','Birinci satır\nİkinci satır',filedata=base64.b64encode(b'Birinci satir').decode(),mime='text/plain')
        first=enqueue(c,u,{'text':'Bunu incele','document_ids':[did]},'telegram','first-file')
        second=enqueue(c,u,{'text':'Bu dosyanın ikinci satırında ne var?','conversation_id':str(first['conversation_id'])},'telegram','file-followup')
        jobrow=c.execute('SELECT prompt FROM jobs WHERE id=%s',(second['id'],)).fetchone()
    prompt=decrypt(jobrow['prompt'])
    assert prompt['documents'][0]['source_id']==did

def test_invitation_is_queued_and_sent_by_system_mailer(monkeypatch):
    import tool_broker
    sent=[]
    class SMTP:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def login(self,user,password):assert user=='info@inovensai.com' and password=='test-password'
        def send_message(self,message):sent.append(message);return {}
    monkeypatch.setenv('INVITATION_SMTP_HOST','inovensai.com');monkeypatch.setenv('INVITATION_SMTP_USER','info@inovensai.com');monkeypatch.setenv('INVITATION_SMTP_PASSWORD','test-password')
    monkeypatch.setattr(tool_broker.smtplib,'SMTP_SSL',SMTP)
    result=dispatch(1,'invitations','POST',{'email':'newmember@example.com','role':'member'})
    assert result['email_queued'] is True
    from portal import sync_profiles
    sync_profiles([{'id':5,'email':'newmember@example.com','name':'New Member'}])
    with connect() as c:assert c.execute('SELECT email_status FROM invitations WHERE email=%s',('newmember@example.com',)).fetchone()['email_status']=='pending'
    tool_broker.send_pending_invitation()
    with connect() as c:invitation=c.execute('SELECT email_status,email_sent_at FROM invitations WHERE email=%s',('newmember@example.com',)).fetchone()
    assert invitation['email_status']=='sent' and invitation['email_sent_at'] is not None
    assert len(sent)==1 and sent[0]['To']=='newmember@example.com'
    assert 'https://inovensai.com/panel/' in sent[0].get_content() and '/kisisel' in sent[0].get_content()
    sync_profiles([{'id':5,'email':'newmember@example.com','name':'New Member'}])
    with connect() as c:assert not c.execute('SELECT 1 FROM invitations WHERE email=%s',('newmember@example.com',)).fetchone()

def test_generated_workspace_file_becomes_chat_attachment():
    import runner,tool_broker
    jid,_=job(4)
    with connect() as c:u=user(c,4);row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    home=tool_broker.ROOT/'users'/'4'/'personal-worker'/'0';workspace=home/'workspace';workspace.mkdir(parents=True)
    (workspace/'report.txt').write_text('Synthetic report',encoding='utf-8')
    with connect() as c:
        text,attachments=runner.collect_output_documents(c,u,row,home,'Hazır.\nMEDIA:/workspace/report.txt',[],'personal')
        document=c.execute('SELECT name,mime FROM documents WHERE id=%s',(attachments[0]['id'],)).fetchone()
    assert text=='Hazır.\n\n📎 report.txt · 0 KB\nKalite kontrolü: geçti'
    assert document=={'name':'report.txt','mime':'text/plain'}

def test_delivery_hides_internal_paths_and_long_source_dump():
    import runner
    raw='Rapor hazırlandı.\nTelegram bağlantısı olmadığı için buradan teslim ediyorum.\n/workspace/report.pdf\nKaynaklar:\nhttps://example.com/a\nDrive\'a yükleyeyim mi?'
    item={'name':'report.pdf','size':65536,'pages':12}
    text=runner.compact_delivery(raw,[item])
    assert '/workspace/' not in text and 'https://' not in text and 'yükleyeyim mi' not in text and 'Telegram bağlantısı' not in text
    assert '📎 report.pdf · 12 sayfa · 64 KB' in text and 'Kalite kontrolü: geçti' in text

def test_pdf_quality_gate_rejects_single_line_overflow(tmp_path):
    import fitz
    from document_qa import validate_document
    bad=tmp_path/'bad.pdf';document=fitz.open();page=document.new_page()
    for offset in range(30):page.insert_text((30+offset,30),'Broken overlapping layout',fontsize=10)
    document.save(bad);document.close()
    result=validate_document(bad,tmp_path/'previews')
    assert result['ok'] is False
    assert any(reason.endswith(('tek_satira_sikismis','dikey_yayilim_yetersiz','icerik_tasiyor')) for reason in result['reasons'])
    assert result['screenshots']

def test_pdf_quality_gate_renders_readable_pages(tmp_path):
    import fitz
    from document_qa import validate_document
    good=tmp_path/'good.pdf';document=fitz.open();page=document.new_page();page.insert_textbox(fitz.Rect(50,50,545,790),'Readable report line.\n'*30,fontsize=11);document.save(good);document.close()
    result=validate_document(good,tmp_path/'previews')
    assert result['ok'] is True and result['pages']==1
    assert Path(result['screenshots'][0]).is_file()

def test_telegram_delivery_sends_stored_attachment(monkeypatch):
    import telegram_bot
    with connect() as c:
        c.execute('UPDATE platform_users SET telegram_id=2222 WHERE id=2')
        did=add_document(c,user(c,2),'report.txt','Synthetic report',filedata=base64.b64encode(b'Synthetic report').decode(),mime='text/plain')
        c.execute('INSERT INTO notifications(user_id,body) VALUES(2,%s)',(encrypt({'text':'Hazır.','attachments':[{'id':did,'name':'report.txt','mime':'text/plain'}]}),))
    calls=[]
    monkeypatch.setattr(telegram_bot,'send',lambda chat,text:calls.append(('text',chat,text)))
    monkeypatch.setattr(telegram_bot,'send_attachment',lambda chat,name,raw,mime:calls.append(('file',chat,name,raw,mime)))
    telegram_bot.deliver()
    assert calls==[('text',2222,'Hazır.'),('file',2222,'report.txt',b'Synthetic report','text/plain')]
    with connect() as c:assert c.execute('SELECT status FROM notifications').fetchone()['status']=='sent'

def test_public_no_tool_prompt_uses_fast_path():
    from quick_reply import eligible
    jid,_=job(2)
    with connect() as c:
        c.execute('UPDATE jobs SET prompt=%s WHERE id=%s',(encrypt({'text':'Topluluk etkinliği için üç kısa fikir öner.'}),jid))
        item=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    assert eligible(item)

def test_archive_and_tool_requests_stay_on_agent():
    from quick_reply import eligible
    for text in ['Topluluk tüzüğünü özetle.','Topluluk Drive dosyasını indir.']:
        jid,_=job(2)
        with connect() as c:
            c.execute('UPDATE jobs SET prompt=%s WHERE id=%s',(encrypt({'text':text}),jid))
            item=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
        assert not eligible(item)

def test_telegram_link_is_created_for_requesting_user(monkeypatch):
    monkeypatch.setenv('TELEGRAM_USERNAME','inovens_bot')
    first=dispatch(2,'telegram/link','POST',{})
    second=dispatch(3,'telegram/link','POST',{})
    assert first['url'].startswith('https://t.me/inovens_bot?start=')
    assert second['url'].startswith('https://t.me/inovens_bot?start=')
    assert first['url']!=second['url']
    with connect() as c:
        assert {row['user_id'] for row in c.execute('SELECT user_id FROM link_tokens').fetchall()}=={2,3}

def test_telegram_requires_button_consent_before_processing(monkeypatch):
    import archive,telegram_bot
    sent=[]
    monkeypatch.setattr(telegram_bot,'send',lambda chat,text,reply_markup=None:sent.append((chat,text,reply_markup)))
    monkeypatch.setattr(telegram_bot,'api',lambda method,body:True)
    with connect() as c:c.execute('UPDATE platform_users SET telegram_id=2002,telegram_consent_version=NULL WHERE id=2')
    telegram_bot.update({'update_id':510,'message':{'chat':{'type':'private'},'from':{'id':2002},'text':'selam'}})
    assert sent[-1][2]['inline_keyboard'][0][0]['callback_data']=='consent:accept'
    with connect() as c:assert not c.execute('SELECT 1 FROM jobs WHERE user_id=2').fetchone()
    telegram_bot.update({'update_id':511,'callback_query':{'id':'callback-1','from':{'id':2002},'data':'consent:accept'}})
    with connect() as c:assert c.execute('SELECT telegram_consent_version FROM platform_users WHERE id=2').fetchone()['telegram_consent_version']==archive.TG_CONSENT

def test_telegram_fallback_code_links_requesting_user(monkeypatch):
    import telegram_bot
    sent=[]
    monkeypatch.setattr(telegram_bot,'send',lambda chat,text:sent.append((chat,text)))
    telegram_bot.update({'update_id':500,'message':{'chat':{'type':'private'},'from':{'id':1395766223},'text':'/topluluk'}})
    with connect() as c:code=c.execute('SELECT code FROM telegram_pair_codes WHERE telegram_id=1395766223').fetchone()['code']
    assert code in sent[-1][1]
    result=dispatch(4,'telegram/pair-code','POST',{'code':code.lower()})
    assert result['telegram_id']==1395766223
    with connect() as c:
        assert c.execute('SELECT telegram_id FROM platform_users WHERE id=4').fetchone()['telegram_id']==1395766223
        assert c.execute('SELECT used_at FROM telegram_pair_codes WHERE code=%s',(code,)).fetchone()['used_at'] is not None
    with pytest.raises(Denied):dispatch(2,'telegram/pair-code','POST',{'code':code})

def test_board_roles_have_community_and_google_access_without_owner_admin(monkeypatch):
    import tool_broker
    with connect() as c:
        board_user=user(c,4)
        board_user['google_access']=False
        tool_broker.google_permission(board_user)
        document=add_document(c,board_user,'YK belgesi','Topluluk etkinliği.',scope='board')
        assert can_document(c,board_user,c.execute('SELECT * FROM documents WHERE id=%s',(document,)).fetchone())
    with pytest.raises(Denied) as settings_denied:dispatch(4,'settings','GET',{})
    with pytest.raises(Denied) as users_denied:dispatch(4,'users','GET',{})
    assert settings_denied.value.code==users_denied.value.code=='owner_required'

def test_telegram_board_scope_is_shared_only_for_authorized_roles(monkeypatch):
    import telegram_bot
    sent=[]
    monkeypatch.setattr(telegram_bot,'send',lambda chat,text:sent.append((chat,text)))
    with connect() as c:
        c.execute('UPDATE platform_users SET telegram_id=4004 WHERE id=4')
        c.execute('UPDATE platform_users SET telegram_id=2002 WHERE id=2')
    telegram_bot.update({'update_id':41,'message':{'chat':{'type':'private'},'from':{'id':4004},'text':'/topluluk'}})
    with connect() as c:assert c.execute('SELECT scope FROM telegram_preferences WHERE user_id=4').fetchone()['scope']=='community'
    assert 'Topluluk alanına geçildi' in sent[-1][1]
    telegram_bot.update({'update_id':42,'message':{'chat':{'type':'private'},'from':{'id':2002},'text':'/topluluk'}})
    with pytest.raises(Denied):telegram_bot.update({'update_id':43,'message':{'chat':{'type':'private'},'from':{'id':2002},'text':'/yk'}})

def test_telegram_board_test_mail_creates_approval_without_model_job(monkeypatch):
    import telegram_bot
    sent=[]
    monkeypatch.setattr(telegram_bot,'send',lambda chat,text:sent.append((chat,text)))
    with connect() as c:
        c.execute('UPDATE platform_users SET telegram_id=4004 WHERE id=4')
        c.execute("INSERT INTO telegram_preferences(user_id,scope) VALUES(4,'board')")
        before=c.execute('SELECT count(*) n FROM jobs').fetchone()['n']
    telegram_bot.update({'update_id':43,'message':{'chat':{'type':'private'},'from':{'id':4004},'text':'topluluk mailinden test@example.invalid adresine test maili gönder'}})
    with connect() as c:
        approval=c.execute("SELECT kind,status FROM action_approvals WHERE user_id=4 ORDER BY created_at DESC LIMIT 1").fetchone()
        assert approval=={'kind':'gmail_send','status':'pending'}
        assert c.execute('SELECT count(*) n FROM jobs').fetchone()['n']==before
    assert 'önizlemesi hazır' in sent[-1][1]

def test_member_still_cannot_use_admin_or_google():
    import tool_broker
    with connect() as c:member=user(c,2)
    with pytest.raises(Denied):tool_broker.google_permission(member)
    with pytest.raises(Denied):dispatch(2,'settings','GET',{})

def test_grant_only_target_job():
    j,_=job();j2,_=job()
    with connect() as c:c.execute('UPDATE platform_users SET daily_limit=0 WHERE id=2')
    dispatch(1,'jobs/'+j+'/grant','POST',{'amount':1000});reserve(2,j,'m','t',100,10,PRICE)
    with pytest.raises(Denied):reserve(2,j2,'m','t',100,10,PRICE)

def test_token_normalization():
    assert normalize_usage({'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':50},'completion_tokens_details':{'reasoning_tokens':5}})=={'input':100,'output':20,'cached':50,'reasoning':5}
    assert normalize_usage({'promptTokenCount':100,'candidatesTokenCount':20,'thoughtsTokenCount':5})['output']==25

def test_approval_cannot_be_confirmed_by_another_user():
    aid=str(uuid.uuid4())
    with connect() as c:c.execute('INSERT INTO action_approvals(id,user_id,kind,preview,payload,expires_at) VALUES(%s,%s,%s,%s,%s,%s)',(aid,4,'gmail_send',encrypt({'to':['a@example.com']}),encrypt({}),next_reset()))
    with pytest.raises(Denied):dispatch(1,'approvals/'+aid,'POST',{'approved':True})
    dispatch(4,'approvals/'+aid,'POST',{'approved':True})

def test_member_cannot_call_google_broker():
    from tool_broker import call
    j,_=job()
    with connect() as c:u=user(c,2);row=c.execute('SELECT * FROM jobs WHERE id=%s',(j,)).fetchone()
    with pytest.raises(Denied) as e:call(u,row,'gmail_search',{'query':'test'})
    assert e.value.code=='board_required'

def test_role_change_rotates_workspace():
    dispatch(1,'users/4','PATCH',{'role':'member'})
    with connect() as c:assert user(c,4)['workspace_epoch']==1

def test_delete_rotates_workspace():
    _,cid=job();dispatch(2,'conversations/'+cid,'DELETE',{})
    with connect() as c:assert user(c,2)['workspace_epoch']==1

def test_user_can_rename_and_hide_own_job_without_exposing_title_to_board():
    jid,_=job()
    dispatch(2,'jobs/'+jid,'PATCH',{'title':'Haftalık araştırma raporu'})
    own=next(item for item in dispatch(2,'jobs','GET',{})['items'] if str(item['id'])==jid)
    owner_view=next(item for item in dispatch(1,'jobs','GET',{'all':True})['items'] if str(item['id'])==jid)
    assert own['title']=='Haftalık araştırma raporu'
    assert owner_view['title'] is None
    with pytest.raises(Denied):dispatch(4,'jobs/'+jid,'PATCH',{'title':'Başkasının özel işi'})
    dispatch(2,'jobs/'+jid,'DELETE',{})
    assert all(str(item['id'])!=jid for item in dispatch(2,'jobs','GET',{})['items'])
    with connect() as c:
        hidden=c.execute('SELECT hidden_at,cancel_requested FROM jobs WHERE id=%s',(jid,)).fetchone()
        assert hidden['hidden_at'] is not None and hidden['cancel_requested'] is True

def test_board_can_edit_and_delete_community_operations():
    created=dispatch(4,'operations','POST',{'title':'Etkinlik planı','owner_name':'Yusuf','start_date':'2026-09-18','due_date':'2026-09-20','note':'İlk taslak','scope':'board'})['items'][0]
    dispatch(4,'operations/'+str(created['id']),'PATCH',{'title':'Etkinlik uygulaması','owner_name':'Yönetim','start_date':'2026-09-19','due_date':'2026-09-21','status':'running','note':'Salon teyidi bekleniyor','scope':'community'})
    updated=next(item for item in dispatch(2,'operations','GET',{})['items'] if item['id']==created['id'])
    assert updated['title']=='Etkinlik uygulaması' and str(updated['start_date'])=='2026-09-19' and updated['status']=='running' and updated['scope']=='community'
    with pytest.raises(Denied):dispatch(4,'operations/'+str(created['id']),'PATCH',{'start_date':'2026-09-25','due_date':'2026-09-21'})
    with pytest.raises(Denied):dispatch(2,'operations/'+str(created['id']),'PATCH',{'title':'Yetkisiz değişiklik'})
    dispatch(4,'operations/'+str(created['id']),'DELETE',{})
    assert all(item['id']!=created['id'] for item in dispatch(4,'operations','GET',{})['items'])

def test_operation_assignment_respects_visibility():
    with pytest.raises(Denied) as denied:
        dispatch(4,'operations','POST',{'title':'YK işi','scope':'board','assignee_user_id':2})
    assert denied.value.code=='invalid_assignee'
    created=dispatch(4,'operations','POST',{'title':'Açık etkinlik','scope':'community','assignee_user_id':2})['items'][0]
    assert created['assignee_user_id']==2 and created['assignee_name']=='Test 2'
    with pytest.raises(Denied):dispatch(4,'operations/'+str(created['id']),'PATCH',{'scope':'board'})
    with pytest.raises(Denied):dispatch(2,'operations/'+str(created['id']),'PATCH',{'status':'done'})

def test_operation_due_reminder_is_queued_once_for_linked_assignee():
    from datetime import timedelta
    from main import send_due_operation_reminders
    due=(day_key()+timedelta(days=1)).isoformat()
    with connect() as c:c.execute('UPDATE platform_users SET telegram_id=12345 WHERE id=2')
    created=dispatch(4,'operations','POST',{'title':'Sentetik etkinlik','scope':'community','assignee_user_id':2,'due_date':due})['items'][0]
    send_due_operation_reminders();send_due_operation_reminders()
    with connect() as c:
        rows=c.execute('SELECT body FROM notifications WHERE user_id=2').fetchall()
        reminders=c.execute('SELECT kind FROM operation_reminders WHERE operation_id=%s',(created['id'],)).fetchall()
    assert len(rows)==1 and len(reminders)==1 and reminders[0]['kind']=='tomorrow'
    assert 'Sentetik etkinlik' in decrypt(rows[0]['body'])
    dispatch(4,'operations/'+str(created['id']),'PATCH',{'status':'done'})
    send_due_operation_reminders()
    with connect() as c:assert c.execute('SELECT count(*) n FROM notifications WHERE user_id=2').fetchone()['n']==1

def test_board_decisions_are_archived_and_board_only():
    created=dispatch(4,'decisions','POST',{'meeting_date':'2026-09-14','title':'Etkinlik takvimi','decision':'Ekim ayında iki etkinlik yapılacak.','status':'accepted'})['items'][0]
    dispatch(4,'decisions/'+str(created['id']),'PATCH',{'status':'implemented','decision':'Ekim ayındaki iki etkinlik takvime işlendi.'})
    updated=next(item for item in dispatch(4,'decisions','GET',{})['items'] if item['id']==created['id'])
    assert updated['status']=='implemented' and 'takvime işlendi' in updated['decision']
    with pytest.raises(Denied):dispatch(2,'decisions','GET',{})
    dispatch(4,'decisions/'+str(created['id']),'DELETE',{})
    assert all(item['id']!=created['id'] for item in dispatch(4,'decisions','GET',{})['items'])

def test_member_cannot_control_system():
    with pytest.raises(Denied):dispatch(2,'system/actions','POST',{'action':'restart_router'})

def test_owner_invitation_applies_to_pending_identity():
    from portal import sync_profiles
    with connect() as c:
        c.execute("UPDATE platform_users SET status='pending' WHERE id=3")
        c.execute('INSERT INTO invitations(email,role,created_by) VALUES(%s,%s,%s)',('3@test.invalid','board',1))
    sync_profiles([{'id':3,'email':'3@test.invalid','name':'Invited'}])
    with connect() as c:u=user(c,3)
    assert u['status']=='active' and u['role']=='board'

def test_google_send_waits_for_requester_and_runs_once(monkeypatch):
    import tool_broker
    j,_=job(4)
    with connect() as c:
        c.execute('UPDATE platform_users SET google_access=true WHERE id=4')
        c.execute("INSERT INTO settings VALUES('google_routing','{\"owner_confirmed_no_training\":true}')")
        u=user(c,4);row=c.execute('SELECT * FROM jobs WHERE id=%s',(j,)).fetchone()
    calls=[]
    class FakeGoogle:
        def call(self,name,args):calls.append((name,args));return {'data':{'id':'test-only'}}
    monkeypatch.setattr(tool_broker,'google_module',lambda:FakeGoogle())
    r=tool_broker.call(u,row,'gmail_send',{'to':['test@example.com'],'subject':'Synthetic','body':'No message is sent by this test.'})
    assert not calls
    tool_broker.execute_approvals();assert not calls
    dispatch(4,'approvals/'+r['approval_id'],'POST',{'approved':True})
    tool_broker.execute_approvals();tool_broker.execute_approvals()
    assert len(calls)==1

def test_workspace_pdf_can_be_uploaded_and_sent_as_real_attachment(monkeypatch):
    import tool_broker
    jid,_=job(4)
    with connect() as c:
        u=user(c,4);row=c.execute('SELECT j.*,cv.google_bound,cv.scope FROM jobs j JOIN conversations cv ON cv.id=j.conversation_id WHERE j.id=%s',(jid,)).fetchone()
    workspace=tool_broker.ROOT/'users'/'4'/'personal-worker'/'0'/'workspace'
    workspace.mkdir(parents=True)
    report=workspace/'report.txt';report.write_text('Synthetic report',encoding='utf-8')
    upload=tool_broker.call(u,row,'drive_upload',{'localPath':'/workspace/report.txt'})
    mail=tool_broker.call(u,row,'gmail_send',{'to':['test@example.invalid'],'subject':'Synthetic attachment','body':'Attached.','attachments':['/workspace/report.txt']})
    with connect() as c:
        up=decrypt(c.execute('SELECT payload FROM action_approvals WHERE id=%s',(upload['approval_id'],)).fetchone()['payload'])
        sent=c.execute('SELECT preview,payload FROM action_approvals WHERE id=%s',(mail['approval_id'],)).fetchone()
        preview,payload=decrypt(sent['preview']),decrypt(sent['payload'])
        owned=c.execute('SELECT name FROM documents WHERE id=%s AND owner_id=4',(up['documentId'],)).fetchone()
        assert owned
        assert owned['name']=='report.txt'
    assert preview['attachments']==['report.txt'] and len(payload['documentIds'])==1

def test_workspace_attachment_rejects_path_escape():
    import tool_broker
    jid,_=job(4)
    with connect() as c:
        u=user(c,4);row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    with pytest.raises(Denied) as error:
        tool_broker.call(u,row,'drive_upload',{'localPath':'/workspace/../../secret.txt'})
    assert error.value.code=='file_denied'

def test_approved_mail_stages_attachment_for_google(monkeypatch):
    import tool_broker
    jid,_=job(4)
    with connect() as c:
        u=user(c,4);row=c.execute('SELECT * FROM jobs WHERE id=%s',(jid,)).fetchone()
    workspace=tool_broker.ROOT/'users'/'4'/'personal-worker'/'0'/'workspace';workspace.mkdir(parents=True)
    (workspace/'report.txt').write_text('Synthetic report',encoding='utf-8')
    approval=tool_broker.call(u,row,'gmail_send',{'to':['test@example.invalid'],'subject':'Synthetic attachment','body':'Attached.','attachments':['/workspace/report.txt']})
    calls=[]
    class FakeGoogle:
        def call(self,name,args):
            assert name=='gmail_send' and len(args['attachments'])==1
            assert Path(args['attachments'][0]).name=='report.txt'
            assert Path(args['attachments'][0]).read_text()=='Synthetic report'
            calls.append(args);return {'data':{'id':'test-only'}}
    monkeypatch.setattr(tool_broker,'google_module',lambda:FakeGoogle())
    dispatch(4,'approvals/'+approval['approval_id'],'POST',{'approved':True})
    tool_broker.execute_approvals()
    assert len(calls)==1 and not Path(calls[0]['attachments'][0]).exists()


def test_php_empty_object_roundtrip():
    assert dispatch(2,'jobs','GET',[])['items']==[]
    with pytest.raises(Denied) as e:dispatch(2,'jobs','GET',['invalid'])
    assert e.value.code=='invalid_body'


def test_owner_unlimited_tokens_but_usage_recorded():
    j,_=job(1)
    with connect() as c:c.execute('UPDATE platform_users SET daily_limit=0 WHERE id=1')
    call=reserve(1,j,'m','t',100,10,PRICE)
    settle(call,{'input':80,'output':10,'cached':0,'reasoning':0})
    with connect() as c:q=usage_view(c,1)
    assert q['unlimited'] is True and q['limit'] is None and q['remaining'] is None
    assert q['used']==90 and q['reserved']==0

def test_member_still_limited():
    j,_=job(2)
    with connect() as c:c.execute('UPDATE platform_users SET daily_limit=0 WHERE id=2')
    with pytest.raises(Denied) as e:reserve(2,j,'m','t',100,10,PRICE)
    assert e.value.code=='daily_token_limit'


def test_quick_greeting_requires_no_tools_or_attachments():
    from quick_reply import eligible
    from execution_contract import infer
    assert eligible({'prompt':encrypt({'text':'Selam!'})})
    assert not eligible({'prompt':encrypt({'text':'selam, maillerime bak'})})
    assert not eligible({'prompt':encrypt({'text':'selam','documents':[{'text':'private'}]})})
    assert not eligible({'prompt':encrypt({'text':'selam','images':['image']})})
    assert not eligible({'prompt':encrypt({'text':'Bir afiş oluştur','execution_contract':infer('Bir afiş oluştur','board')})})


def test_archive_only_when_requested():
    from retrieval_policy import allows_archive
    for text in ['selam','nasılsın','2+2 kaç','INOVENS için üç etkinlik fikri yaz','Python nedir?','Bu metni düzelt','Topluluk için duyuru yaz']:
        assert not allows_archive({'prompt':encrypt({'text':text})}),text
    for text in ['Tüzükte üyelik koşulları neler?','Kayıtlı notlarımda ara','Geçen toplantı kararlarını bul','Arşivden başkanın adını bul']:
        assert allows_archive({'prompt':encrypt({'text':text})}),text
    assert not allows_archive({'prompt':encrypt({'text':'Arşive bakma, genel bir fikir ver'})})

def test_archive_tool_blocks_generic_request_before_search(monkeypatch):
    import tool_broker
    j,_=job()
    with connect() as c:
        c.execute('UPDATE jobs SET prompt=%s WHERE id=%s',(encrypt({'text':'Genel bir etkinlik fikri yaz'}),j))
        u=user(c,2);row=c.execute('SELECT * FROM jobs WHERE id=%s',(j,)).fetchone()
    def forbidden(*args,**kwargs):raise AssertionError('Archive was queried')
    monkeypatch.setattr(tool_broker,'search_documents',forbidden)
    with pytest.raises(Denied) as e:tool_broker.call(u,row,'knowledge_search',{'query':'anything'})
    assert e.value.code=='archive_not_requested'

def test_web_search_falls_back_to_rss_when_html_search_is_empty(monkeypatch):
    import tool_broker
    class Response:
        def __init__(self,status_code=200,text='',content=b''):
            self.status_code=status_code;self.text=text;self.content=content
    rss=b'<?xml version="1.0"?><rss><channel><item><title>Guncel yapay zeka haberi</title><link>https://example.com/ai-news</link></item></channel></rss>'
    calls=[]
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url):
            calls.append(url)
            return Response(202,'challenge') if 'duckduckgo' in url else Response(200,content=rss)
    monkeypatch.setattr(tool_broker.httpx,'Client',Client)
    monkeypatch.setattr(tool_broker,'public_url',lambda url:(None,None))
    result=tool_broker.web_search('bugünün yapay zeka gelişmeleri')
    assert result['results'][0]['url']=='https://example.com/ai-news'
    assert any('bing.com' in url for url in calls)

def test_web_fetch_http_error_is_recoverable(monkeypatch):
    import tool_broker
    class Response:
        status_code=403;is_redirect=False
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def stream(self,*args,**kwargs):return Response()
    monkeypatch.setattr(tool_broker.httpx,'Client',Client)
    monkeypatch.setattr(tool_broker,'public_url',lambda url:(type('P',(),{'scheme':'https','hostname':'example.com','netloc':'example.com','path':'/news','query':''})(),'203.0.113.1'))
    result=tool_broker.web_fetch('https://example.com/news')
    assert result['available'] is False and result['recoverable'] is True and result['http_status']==403

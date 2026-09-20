import hashlib,json,re,socket,sqlite3,subprocess,time,uuid
import httpx
from common import *

FIXER_MODEL='inovens-panel-fixer'
SERVICES={
    'main_router':'9router.service','general_router':'inovens-router-general.service',
    'proxy':'inovens-platform-proxy.service','egress':'inovens-egress.service','ai_ege':'openclaw-gateway.service'
}
SAFE_SERVICES=set(SERVICES.values())
SECRET=re.compile(r'(?i)(bearer\s+|api[_-]?key\s*[=:]\s*|token\s*[=:]\s*|password\s*[=:]\s*)\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
JOURNAL_ERRORS=re.compile(r'(?i)MissingSessionID|HTTP\s+5\d\d|MCP error\s+-?\d+|delegated task failed|provider[_ ](?:error|fail)|rate.?limit|\b(?:timeout|timed\s+out)\b')
_journal_since=time.time()

def clean(value):
    if isinstance(value,dict):return {str(k)[:80]:clean(v) for k,v in list(value.items())[:30] if str(k).lower() not in {'prompt','body','content','authorization','cookie','file'}}
    if isinstance(value,list):return [clean(v) for v in value[:20]]
    if isinstance(value,str):return SECRET.sub(lambda m:(m.group(1) or '')+'[masked]' if m.group(1) else '[masked-email]',value)[:1200]
    return value if isinstance(value,(int,float,bool)) or value is None else str(value)[:300]

def record(source,code,summary,detail=None,severity='error',related_job=None,connection=None,fingerprint_key=''):
    safe=clean(detail or {});fingerprint=hashlib.sha256(f'{source}|{code}|{fingerprint_key or safe.get("service","")}'.encode()).hexdigest()
    own=connection is None;c=connection or connect()
    try:
        row=c.execute("""INSERT INTO incidents(id,fingerprint,source,severity,code,summary,detail,related_job)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(fingerprint) DO UPDATE SET
          severity=excluded.severity,summary=excluded.summary,detail=excluded.detail,related_job=COALESCE(excluded.related_job,incidents.related_job),
          status=CASE WHEN incidents.status='fixing' THEN 'fixing' ELSE 'open' END,occurrences=incidents.occurrences+1,last_seen=now(),resolved_at=NULL RETURNING id""",
          (str(uuid.uuid4()),fingerprint,str(source)[:80],severity,str(code)[:120],str(summary)[:300],dumps(safe),related_job)).fetchone()
        if own:c.commit()
        return str(row['id'])
    except Exception:
        if own:c.rollback()
        return None
    finally:
        if own:c.close()

def health():
    services={}
    for key,unit in SERVICES.items():
        services[key]=subprocess.run(['systemctl','--user','is-active','--quiet',unit]).returncode==0
    ports={str(port):port_up(port) for port in (20128,20129)}
    proxy=Path(ROOT/'socket/platform.sock').exists()
    database=False
    try:
        with connect() as c:c.execute('SELECT 1').fetchone();database=True
    except Exception:pass
    return {'services':services,'ports':ports,'proxy_socket':proxy,'database':database}

def port_up(port):
    try:
        with socket.create_connection(('127.0.0.1',port),.8):return True
    except OSError:return False

def model_probe(model=FIXER_MODEL,port=20129):
    payload={'model':model,'messages':[{'role':'user','content':'Yanıt olarak yalnız FIXER_OK yaz.'}],'max_tokens':24,'stream':False}
    r=httpx.post(f'http://127.0.0.1:{port}/v1/chat/completions',headers={'Authorization':'Bearer '+os.environ['ROUTER_KEY'],'User-Agent':'inovens-panel-fixer/1.0'},json=payload,timeout=60,trust_env=False)
    text='';data={}
    if r.status_code<400:
        data=r.json();message=(data.get('choices') or [{}])[0].get('message',{});text=str(message.get('content') or message.get('reasoning_content') or '')
    return {'ok':r.status_code==200 and bool(text.strip()),'http':r.status_code,'model':str(data.get('model') or model)[:120],'reply':text[:80]}

def main_router_probe():
    with sqlite3.connect('/home/ege/.9router/db/data.sqlite') as c:row=c.execute('SELECT key FROM apiKeys WHERE isActive=1 ORDER BY createdAt LIMIT 1').fetchone()
    if not row:return {'ok':False,'http':0,'model':'opencode-go/minimax-m3','reply':'no_active_api_key'}
    payload={'model':'opencode-go/minimax-m3','messages':[{'role':'user','content':'Reply with exactly: ROUTER_OK'}],'max_tokens':24,'stream':False}
    r=httpx.post('http://127.0.0.1:20128/v1/chat/completions',headers={'Authorization':'Bearer '+row[0],'User-Agent':'inovens-panel-fixer/1.0'},json=payload,timeout=60,trust_env=False)
    data=r.json() if r.status_code<400 else {};text=str((data.get('choices') or [{}])[0].get('message',{}).get('content') or '')
    return {'ok':r.status_code==200 and bool(text.strip()),'http':r.status_code,'model':str(data.get('model') or 'opencode-go/minimax-m3')[:120],'reply':text[:80]}

def candidates(incident):
    code=incident['code'].casefold();source=incident['source'].casefold()
    if code=='self_test':return ['verify_all']
    if code=='service_down':return ['restart_service','verify_all']
    if source=='ai-ege':return ['restart_service','verify_all']
    if related_retriable(code) and incident.get('related_job'):return ['restart_router_and_retry','retry_job','verify_all']
    if any(x in code for x in ['provider','session','route','model']):return ['restart_router','verify_all']
    if 'telegram' in code or 'telegram' in source:return ['verify_all']
    return ['verify_all']

def related_retriable(code):return code in {'worker_error','empty_worker_response','provider_error','provider_connection','provider_rate_limit','job_time_limit','service_restarted','stale_job'}

def plan_with_model(incident,allowed):
    prompt={'task':'Select one safe repair action and explain briefly in Turkish. Return JSON only.',
      'incident':{'source':incident['source'],'code':incident['code'],'summary':incident['summary'],'detail':clean(incident['detail'])},
      'allowed_actions':allowed,'schema':{'action':'one allowed action','reason':'short text'}}
    payload={'model':FIXER_MODEL,'messages':[{'role':'user','content':dumps(prompt)}],'max_tokens':300,'temperature':0,'stream':False}
    r=httpx.post('http://127.0.0.1:20129/v1/chat/completions',headers={'Authorization':'Bearer '+os.environ['ROUTER_KEY'],'User-Agent':'inovens-panel-fixer/1.0'},json=payload,timeout=90,trust_env=False)
    r.raise_for_status();message=r.json()['choices'][0]['message'];text=str(message.get('content') or message.get('reasoning_content') or '').strip();text=re.sub(r'^```(?:json)?|```$','',text,flags=re.I).strip();plan=json.loads(text)
    if plan.get('action') not in allowed:raise ValueError('model_selected_disallowed_action')
    return {'action':plan['action'],'reason':clean(str(plan.get('reason','')))[:400],'model':FIXER_MODEL}

def restart(unit):
    if unit not in SAFE_SERVICES:raise ValueError('service_not_allowed')
    subprocess.run(['systemctl','--user','restart',unit],check=True,timeout=30)
    time.sleep(3)
    return subprocess.run(['systemctl','--user','is-active','--quiet',unit]).returncode==0

def retry_job(jid):
    with connect() as c:
        row=c.execute('SELECT r.prompt,j.status,j.conversation_id FROM job_recovery r JOIN jobs j ON j.id=r.job_id WHERE r.job_id=%s AND r.expires_at>now() FOR UPDATE',(jid,)).fetchone()
        if not row or not row['conversation_id']:return False
        c.execute("UPDATE jobs SET prompt=%s,status='queued',error_code=NULL,cancel_requested=false,started_at=NULL,finished_at=NULL,heartbeat_at=now() WHERE id=%s",(row['prompt'],jid))
        audit(c,None,'incident.retry_job',jid)
    return True

def verify_job(jid,seconds=120):
    end=time.time()+seconds
    while time.time()<end:
        with connect() as c:row=c.execute('SELECT status,error_code,finished_at FROM jobs WHERE id=%s',(jid,)).fetchone()
        if not row:return {'ok':False,'status':'missing'}
        if row['status'] not in ['queued','running']:return {'ok':row['status']=='done','status':row['status'],'error_code':row['error_code']}
        time.sleep(2)
    return {'ok':False,'status':'still_running'}

def execute(run,incident):
    allowed=candidates(incident);model_error=None
    try:plan=plan_with_model(incident,allowed)
    except Exception as exc:
        model_error=type(exc).__name__;plan={'action':allowed[0],'reason':'Model planı alınamadı; güvenli kayıtlı runbook seçildi.','model':FIXER_MODEL}
    before=health();action=plan['action'];applied=[]
    if action in {'restart_router','restart_router_and_retry'}:
        unit='9router.service' if incident['source'] in ['9router','ai-ege'] else 'inovens-router-general.service'
        applied.append({'restart':unit,'ok':restart(unit)})
    elif action=='restart_service':
        unit=SERVICES.get(str(incident['detail'].get('service','')))
        if not unit:raise ValueError('unknown_service')
        applied.append({'restart':unit,'ok':restart(unit)})
    retried=False
    if action in {'retry_job','restart_router_and_retry'}:
        retried=retry_job(str(incident['related_job']));applied.append({'retry_job':retried})
    after=health();probe=main_router_probe() if incident['source'] in ['9router','ai-ege'] else model_probe()
    job_check=verify_job(str(incident['related_job'])) if retried else None
    known=incident['code']=='self_test' or action!='verify_all'
    ok=all(after['services'].values()) and all(after['ports'].values()) and after['proxy_socket'] and probe['ok'] and (job_check is None or job_check['ok']) and known
    evidence={'before':before,'plan_model_error':model_error,'applied':applied,'after':after,'model_probe':probe,'job_check':job_check,'verified_at':now().isoformat()}
    return plan,evidence,ok,'fixed' if ok else 'needs_review'

def process_one():
    with connect() as c:
        run=c.execute("SELECT * FROM repair_runs WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not run:return False
        incident=c.execute('SELECT * FROM incidents WHERE id=%s',(run['incident_id'],)).fetchone()
        c.execute("UPDATE repair_runs SET status='analyzing',started_at=now() WHERE id=%s",(run['id'],))
    try:
        plan,evidence,ok,status=execute(run,incident)
        with connect() as c:
            c.execute('UPDATE repair_runs SET status=%s,plan=%s,evidence=%s,finished_at=now() WHERE id=%s',(status,dumps(plan),dumps(evidence),run['id']))
            c.execute("UPDATE incidents SET status=%s,resolved_at=CASE WHEN %s='resolved' THEN now() ELSE NULL END WHERE id=%s",('resolved' if ok else 'needs_review','resolved' if ok else 'needs_review',incident['id']))
            audit(c,run['requested_by'],'incident.repair_'+status,incident['id'],{'repair_id':str(run['id']),'action':plan['action']})
    except Exception as exc:
        with connect() as c:
            c.execute("UPDATE repair_runs SET status='failed',error=%s,evidence=%s,finished_at=now() WHERE id=%s",(type(exc).__name__,dumps({'verified_at':now().isoformat()}),run['id']))
            c.execute("UPDATE incidents SET status='needs_review' WHERE id=%s",(incident['id'],))
    return True

def monitor():
    reconcile_completed_jobs()
    snap=health()
    for key,active in snap['services'].items():
        if not active:record('health','service_down',f'{key} servisi çalışmıyor.',{'service':key},'critical',fingerprint_key=key)
        else:
            with connect() as c:
                c.execute("UPDATE incidents SET status='resolved',resolved_at=now() WHERE source='health' AND code='service_down' AND status!='resolved' AND detail->>'service'=%s",(key,))
    with connect() as c:
        # Transient transport incidents stay visible in history but leave the open
        # queue after a quiet recovery window. A new failure reopens the fingerprint.
        c.execute("""UPDATE incidents SET status='resolved',resolved_at=now()
          WHERE status='open' AND source IN ('ai-ege','9router','telegram','panel-bridge','web-ui')
          AND last_seen<now()-interval '15 minutes'""")
        for j in c.execute("SELECT id,channel,model FROM jobs WHERE status='running' AND heartbeat_at<now()-interval '90 seconds'").fetchall():
            record('jobs','stale_job','Çalışan işin yaşam sinyali kesildi.',{'channel':j['channel'],'model':j['model']},'error',j['id'],c)
    monitor_journal()

def reconcile_completed_jobs():
    """Close incidents when a Fixer retry finishes just after its bounded wait."""
    with connect() as c:
        rows=c.execute("""SELECT i.id FROM incidents i JOIN jobs j ON j.id=i.related_job
          WHERE j.status='done' AND i.status IN ('open','fixing','needs_review')""").fetchall()
        for row in rows:
            latest=c.execute('SELECT id,status,evidence FROM repair_runs WHERE incident_id=%s ORDER BY created_at DESC LIMIT 1',(row['id'],)).fetchone()
            if latest and latest['status']=='needs_review':
                evidence=dict(latest['evidence'] or {});evidence['late_job_check']={'ok':True,'status':'done','verified_at':now().isoformat()}
                c.execute("UPDATE repair_runs SET status='fixed',evidence=%s WHERE id=%s",(dumps(evidence),latest['id']))
            c.execute("UPDATE incidents SET status='resolved',resolved_at=now() WHERE id=%s",(row['id'],))

def monitor_journal():
    global _journal_since
    since=_journal_since;until=time.time();proc=subprocess.run(['journalctl','--user','-u','9router.service','-u','openclaw-gateway.service','--since','@'+str(int(since)),'--until','@'+str(int(until)),'--output=json','--no-pager'],text=True,capture_output=True,timeout=12)
    _journal_since=until
    if proc.returncode:return
    grouped={}
    for line in proc.stdout.splitlines():
        try:item=json.loads(line);message=str(item.get('MESSAGE',''))
        except Exception:continue
        match=JOURNAL_ERRORS.search(message)
        if not match:continue
        unit=str(item.get('_SYSTEMD_USER_UNIT') or item.get('_SYSTEMD_UNIT') or '')
        source='ai-ege' if 'openclaw' in unit else '9router';code='MissingSessionID' if 'MissingSessionID' in message else 'delegated_task_failed' if 'delegated task failed' in message.casefold() else re.sub(r'\W+','_',match.group(0).casefold()).strip('_')[:80]
        service='ai_ege' if source=='ai-ege' else 'main_router';key=(source,code,service,unit)
        grouped[key]=grouped.get(key,0)+1
    for (source,code,service,unit),count in grouped.items():
        record(source,code,'AI-Ege veya ana model geçidinde istek hatası algılandı.',{'service':service,'unit':unit,'events_in_window':count,'window_seconds':max(1,int(until-since))},'error',fingerprint_key=code)

def loop():
    last=0.0
    while True:
        try:
            process_one()
            if time.monotonic()-last>30:monitor();last=time.monotonic()
        except Exception as exc:print('incident center error',type(exc).__name__,flush=True)
        time.sleep(1)

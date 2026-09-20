import httpx,time,threading
from common import *
from portal import dispatch,sync_profiles

def api(path,body=None):
    with httpx.Client(timeout=45,trust_env=False) as h:
        url=os.environ['PANEL_URL']+'/api/index.php/bridge/platform/'+path
        r=h.request('GET' if body is None else 'POST',url,headers={'Authorization':'Bearer '+os.environ['BRIDGE_TOKEN']},json=body);r.raise_for_status();return r.json()

def sync():
    data=api('sync');sync_profiles(data['users'])
    with connect() as c:
        profiles=c.execute('SELECT id,role,status,telegram_id FROM platform_users').fetchall()
    api('heartbeat',{'users':profiles})

def tick():
    jobs=api('jobs')['jobs'];results=[]
    for j in jobs:
        with connect() as c:cached=c.execute('SELECT response FROM accepted_commands WHERE id=%s',(j['id'],)).fetchone()
        if cached:response=decrypt(cached['response'])
        else:
            try:
                payload=decrypt(j['payload'],os.environ['APP_KEY'])
                if payload['uid']!=j['user_id']:raise Denied('identity_mismatch','Kullanıcı doğrulanamadı.')
                response={'ok':True,'data':dispatch(payload['uid'],payload['path'],payload['method'],payload['body'])}
            except Denied as e:response={'ok':False,'error':e.message,'code':e.code,'status':e.status}
            except Exception as e:
                import traceback
                frames=traceback.extract_tb(e.__traceback__)
                frame=frames[-1] if frames else None
                location=((frame.filename.rsplit('/',1)[-1]+':'+str(frame.lineno)) if frame else 'unknown')
                print('RPC failure', 'request_id='+str(j['id']), type(e).__name__, 'location='+location, flush=True)
                from incident_center import record
                record('panel-rpc','internal_error','Panel isteği işlenirken beklenmeyen hata oluştu.',{'exception':type(e).__name__,'location':location})
                response={'ok':False,'error':'İşlem tamamlanamadı. Girdi ve bağlantıları kontrol edin.','code':'internal_error','status':500}
            with connect() as c:c.execute('INSERT INTO accepted_commands(id,response) VALUES(%s,%s) ON CONFLICT DO NOTHING',(j['id'],encrypt(response)))
        results.append({'id':j['id'],'claim':j['claim'],'response':encrypt(response,os.environ['APP_KEY'])})
    if results:api('results',{'items':results})

def loop():
    last=0
    while True:
        try:
            if time.monotonic()-last>30:sync();last=time.monotonic()
            tick()
        except Exception as e:
            print('bridge unavailable',type(e).__name__,flush=True)
            from incident_center import record
            record('panel-bridge','bridge_unavailable','Web paneli ile sunucu köprüsü bağlantı kuramadı.',{'exception':type(e).__name__})
        time.sleep(1.5)

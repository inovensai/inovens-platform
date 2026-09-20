import asyncio, base64, hashlib, hmac, json, os, time, uuid
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from common import *
from quota import reserve, settle, normalize_usage

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)

def usable_choice(data):
    if not isinstance(data,dict) or not data.get('choices'):return False
    message=data['choices'][0].get('message',{})
    content=message.get('content')
    visible=isinstance(content,str) and bool(content.strip())
    if isinstance(content,list):visible=any(isinstance(x,dict) and str(x.get('text','')).strip() for x in content)
    return visible or bool(message.get('tool_calls')) or bool(message.get('refusal'))

def capability(uid,jid,seconds=2100):
    raw=base64.urlsafe_b64encode(dumps({'uid':uid,'job':str(jid),'exp':int(time.time())+seconds}).encode()).decode().rstrip('=')
    sig=hmac.new(os.environ['CAPABILITY_KEY'].encode(),raw.encode(),hashlib.sha256).hexdigest()
    return raw+'.'+sig

def authenticate(token):
    try:
        raw,sig=token.removeprefix('Bearer ').split('.')
        expect=hmac.new(os.environ['CAPABILITY_KEY'].encode(),raw.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expect):raise ValueError()
        value=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        if value['exp']<time.time():raise ValueError()
        with connect() as c:
            u=user(c,int(value['uid']),consent=True)
            j=c.execute('SELECT j.*,COALESCE(cv.google_bound,false) google_bound FROM jobs j LEFT JOIN conversations cv ON cv.id=j.conversation_id WHERE j.id=%s AND j.user_id=%s',(value['job'],u['id'])).fetchone()
            if not j or j['status']!='running' or j['cancel_requested']:raise ValueError()
        return u,j
    except Denied:raise
    except Exception:raise Denied('invalid_capability','İş yetkisi geçersiz veya süresi dolmuş.',401)

@app.exception_handler(Denied)
async def denied_handler(request,exc):return JSONResponse({'error':{'message':exc.message,'type':exc.code,'code':exc.code}},status_code=exc.status)

@app.get('/health')
def health():return {'ok':True,'service':'inovens-platform','version':'2.0.0'}

@app.post('/v1/chat/completions')
async def completion(req:Request):
    u,j=authenticate(req.headers.get('authorization',''))
    raw=await req.body()
    if len(raw)>2_000_000:raise Denied('context_too_large','Bağlam çok büyük; daha küçük bir iş veya yeni sohbet başlatın.',413)
    b=json.loads(raw)
    if b.get('model') not in MODELS:raise Denied('model_not_allowed','Model bu platformda izinli değil.')
    from data_policy import guard_request,PRIVATE_MODEL,PRIVATE_FALLBACK_MODEL
    with connect() as c:route=guard_request(c,u,j,b)
    # The job, not caller-supplied model or user fields, fixes the selected route.
    requested=b['model']
    selected=(PRIVATE_MODEL if requested==j['model'] else PRIVATE_FALLBACK_MODEL) if route['route']=='private' else requested
    from data_policy import verify_contributor_route,verify_private_route
    verify_private_route(selected) if route['route']=='private' else verify_contributor_route(selected)
    # Only locally classified public content reaches this Contributor route.
    port=20129;price={'input':.10,'output':.20,'cached':.002};actual=selected;provider='owner-private-route' if route['route']=='private' else 'antigravity-primary-meta-fallback'
    output=min(max(int(b.get('max_completion_tokens',b.get('max_tokens',4096)) or 4096),1),8192)
    # A UTF-8 byte bound is conservative across supported text tokenizers. Image requests are bounded as serialized data too.
    image_data=decrypt(j['prompt']).get('images',[])
    input_bound=len(raw)+sum(len(x) for x in image_data)+2048
    cid=reserve(u['id'],str(j['id']),actual,provider,input_bound,output,price);active_cid=cid
    payload={k:v for k,v in b.items() if k in ['messages','tools','tool_choice','temperature','top_p','parallel_tool_calls','response_format']}
    # Only server-owned schemas may carry instruction text to the provider.
    from tool_catalog import trusted_tools
    if 'tools' in payload:payload['tools']=trusted_tools(payload['tools'])
    payload.pop('response_format',None)
    payload.pop('tool_choice',None)
    from runner import SYSTEM
    from execution_contract import system_instruction
    payload['messages']=[{'role':'system','content':SYSTEM+'\n\n'+system_instruction(j)}]+[m for m in payload.get('messages',[]) if m.get('role') not in ['system','developer']]
    from retrieval_policy import allows_archive
    if not allows_archive(j) and payload.get('tools'):
        payload['tools']=[tool for tool in payload['tools'] if tool.get('function',{}).get('name') not in ['platform__knowledge_search','memory_search','memory_get']]
    if image_data:
        for message in reversed(payload.get('messages',[])):
            if message.get('role')=='user':
                content=message.get('content','');parts=[{'type':'text','text':content}] if isinstance(content,str) else content
                message['content']=parts+[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+x}} for x in image_data];break
    payload.update({'model':selected,'stream':False,'max_tokens':output,'reasoning_effort':'minimal' if selected in ['inovens-combo-fast',PRIVATE_MODEL,PRIVATE_FALLBACK_MODEL] else 'medium'})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180,connect=10),trust_env=False) as client:
            res=await client.post(f'http://127.0.0.1:{port}/v1/chat/completions',headers={'Authorization':'Bearer '+os.environ['ROUTER_KEY']},json=payload)
        if res.status_code>=400:
            unbilled=res.status_code in [400,401,403,404,413,422,429]
            code='provider_rate_limit' if res.status_code==429 else 'provider_error'
            settle(cid,error=code,definitely_unbilled=unbilled)
            from incident_center import record
            record('model-proxy',code,'Model sağlayıcısı isteği reddetti.',{'http_status':res.status_code,'model':selected},'error',j['id'])
            raise Denied(code,'Model sağlayıcısı isteği tamamlayamadı. İş kaydı korundu.',502)
        data=res.json();usage=data.get('usage')
        reported=str(data.get('model') or actual)[:180]
        route_provider='antigravity' if reported.startswith(('ag/','gemini')) else 'meta-contributor' if 'muse-spark' in reported else provider
        try:
            with connect() as c:c.execute('UPDATE model_calls SET model=%s,provider=%s WHERE id=%s',(reported,route_provider,cid))
        except Exception:pass # Billing settlement remains authoritative if metadata enrichment fails.
        settle(cid,normalize_usage(usage) if usage else None,error=None if usage else 'missing_usage')
        if not usable_choice(data) and route['route']=='contributor':
            # Some reasoning models report a successful call while returning no
            # visible text or tool call. Recover once through the owner-pinned
            # private combo instead of leaking an empty final to the user.
            verify_private_route(PRIVATE_MODEL)
            active_cid=reserve(u['id'],str(j['id']),PRIVATE_MODEL,'owner-private-recovery',input_bound,output,price)
            recovery={**payload,'model':PRIVATE_MODEL,'reasoning_effort':'minimal'}
            async with httpx.AsyncClient(timeout=httpx.Timeout(180,connect=10),trust_env=False) as client:
                second=await client.post(f'http://127.0.0.1:{port}/v1/chat/completions',headers={'Authorization':'Bearer '+os.environ['ROUTER_KEY']},json=recovery)
            if second.status_code>=400:
                unbilled=second.status_code in [400,401,403,404,413,422,429]
                settle(active_cid,error='provider_rate_limit' if second.status_code==429 else 'provider_error',definitely_unbilled=unbilled)
                raise Denied('provider_error','Model sağlayıcısı kurtarma isteğini tamamlayamadı.',502)
            data=second.json();usage=data.get('usage');reported=str(data.get('model') or PRIVATE_MODEL)[:180]
            try:
                with connect() as c:c.execute('UPDATE model_calls SET model=%s,provider=%s WHERE id=%s',(reported,'owner-private-recovery',active_cid))
            except Exception:pass
            settle(active_cid,normalize_usage(usage) if usage else None,error=None if usage else 'missing_usage')
        if not usable_choice(data):raise Denied('invalid_provider_response','Model geçerli cevap döndürmedi.',502)
        if not b.get('stream'):return JSONResponse(data)
        choice=data['choices'][0];message=choice.get('message',{});delta={'role':'assistant'}
        for key in ['content','reasoning_content','refusal']:
            if message.get(key) is not None:delta[key]=message[key]
        if message.get('tool_calls'):delta['tool_calls']=[{**tc,'index':i} for i,tc in enumerate(message['tool_calls'])]
        async def events():
            common={'id':data.get('id',str(cid)),'object':'chat.completion.chunk','created':data.get('created',int(time.time())),'model':data.get('model',actual)}
            yield 'data: '+dumps({**common,'choices':[{'index':0,'delta':delta,'finish_reason':None}]})+'\n\n'
            yield 'data: '+dumps({**common,'choices':[{'index':0,'delta':{},'finish_reason':choice.get('finish_reason','stop')}],'usage':usage})+'\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(events(),media_type='text/event-stream')
    except Denied:raise
    except BaseException as exc:
        settle(active_cid,error='provider_connection_unknown')
        from incident_center import record
        record('model-proxy','provider_connection','Model sağlayıcısı bağlantısı kesildi.',{'exception':type(exc).__name__,'model':selected},'error',j['id'])
        raise Denied('provider_connection','Model bağlantısı kesildi; kullanım uzlaştırılacak.',502)

@app.post('/tools')
async def tool(req:Request):
    u,j=authenticate(req.headers.get('authorization',''))
    b=await req.json()
    from tool_broker import call
    try:result=await asyncio.to_thread(call,u,j,b.get('name'),b.get('arguments') or {})
    except Denied as exc:
        if exc.status>=500:
            from incident_center import record
            record('tools',exc.code,'Bir araç çağrısı tamamlanamadı.',{'tool':str(b.get('name',''))[:100]},'error',j['id'])
        raise
    except Exception as exc:
        from incident_center import record
        record('tools','tool_internal_error','Bir araç çağrısı beklenmeyen hata verdi.',{'tool':str(b.get('name',''))[:100],'exception':type(exc).__name__},'error',j['id'])
        raise
    from data_policy import guard_tool
    # Check full tool output before a worker can summarize, persist, or forward it.
    guard_tool(u,j,result)
    detail={'tool':str(b.get('name',''))[:100]}
    # Retain source provenance for completion checks without logging page bodies.
    if detail['tool'] in ['web_fetch','browser_open','browser_read']:
        source=result.get('url') if isinstance(result,dict) else None
        if not source and isinstance(b.get('arguments'),dict):source=b['arguments'].get('url')
        if isinstance(source,str) and source.startswith(('http://','https://')):detail['source_url']=source[:1500]
    with connect() as c:audit(c,u['id'],'tool.used',j['id'],detail)
    return JSONResponse(json.loads(dumps(result)))

@app.post('/internal/release-browser')
async def release_browser(req:Request):
    if not hmac.compare_digest(req.headers.get('authorization',''),'Bearer '+os.environ['CAPABILITY_KEY']):raise Denied('invalid_capability','Yetkisiz.',401)
    b=await req.json()
    from browser_broker import release
    release(int(b['uid']));return {'ok':True}

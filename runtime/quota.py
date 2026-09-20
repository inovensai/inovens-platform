"""Atomic per-call reservations. Istanbul date buckets never require a midnight reset job."""
import uuid
from decimal import Decimal, ROUND_CEILING
from common import *

def budget_snapshot(c,at=None):
    d=day_key(at);m=d.replace(day=1)
    r=c.execute('SELECT COALESCE(sum(cost_try+reserved_try) FILTER(WHERE day=%s),0) AS day,COALESCE(sum(cost_try+reserved_try) FILTER(WHERE day>=%s),0) AS month FROM daily_usage',(d,m)).fetchone()
    return {'daily_spend_try':float(r['day']),'monthly_spend_try':float(r['month'])}

def usage_view(c,uid,at=None):
    u=user(c,uid,active=False);d=day_key(at)
    r=c.execute('SELECT * FROM daily_usage WHERE user_id=%s AND day=%s',(uid,d)).fetchone() or {'tokens':0,'reserved_tokens':0,'cost_try':0,'reserved_try':0}
    return {'day':str(d),'timezone':'Europe/Istanbul','resets_at':next_reset(at).isoformat(),'unlimited':u['role']=='owner','limit':None if u['role']=='owner' else daily_limit(u),'used':r['tokens'],'reserved':r['reserved_tokens'],'remaining':None if u['role']=='owner' else max(0,daily_limit(u)-r['tokens']-r['reserved_tokens']),'cost_try':float(r['cost_try']),'reserved_try':float(r['reserved_try'])}

def reserve(uid,job_id,model,provider,input_bound,output_bound,price,at=None):
    amount=int(input_bound)+int(output_bound)
    if amount<=0:raise Denied('invalid_reservation','Token rezervi geçersiz.',422)
    with connect() as c:
        c.execute('SELECT pg_advisory_xact_lock(761904)')
        u=user(c,uid,consent=True);cfg=limits(c)
        if not cfg['enabled']:raise Denied('maintenance','Sistem hazırlanıyor. Yeni işler geçici olarak kapalı.',503)
        j=c.execute('SELECT * FROM jobs WHERE id=%s AND user_id=%s FOR UPDATE',(job_id,uid)).fetchone()
        if not j or j['status']!='running' or j['cancel_requested']:raise Denied('job_stopped','İş artık çalışmıyor.',409)
        if j['call_count'] >= (150 if j['research'] else 30):raise Denied('job_step_limit','İş adım sınırına ulaştı; ara sonuçlar korundu.',429)
        d=day_key(at);m=d.replace(day=1);fx=Decimal(str(cfg['usd_try']))
        cost=((Decimal(input_bound)*Decimal(str(price['input']))+Decimal(output_bound)*Decimal(str(price['output'])))*fx/Decimal(1_000_000)).quantize(Decimal('0.00000001'),rounding=ROUND_CEILING)
        c.execute('INSERT INTO daily_usage(user_id,day) VALUES(%s,%s) ON CONFLICT DO NOTHING',(uid,d))
        r=c.execute('SELECT * FROM daily_usage WHERE user_id=%s AND day=%s FOR UPDATE',(uid,d)).fetchone()
        base=amount if u['role']=='owner' else max(0,min(amount,daily_limit(u)-r['tokens']-r['reserved_tokens']));extra=amount-base;grant=None
        if extra:
            grant=c.execute('SELECT * FROM quota_grants WHERE user_id=%s AND job_id=%s AND expires_at>%s AND amount-used-reserved>=%s ORDER BY created_at FOR UPDATE LIMIT 1',(uid,job_id,at or now(),extra)).fetchone()
            if not grant:raise Denied('daily_token_limit','Günlük token hakkınız yetersiz. Haklar 00.00’da yenilenir.',429)
        b=budget_snapshot(c,at)
        if Decimal(str(b['daily_spend_try']))+cost>Decimal(str(cfg['daily_try'])):raise Denied('daily_budget_limit','Topluluğun günlük model bütçesi doldu.',429)
        if Decimal(str(b['monthly_spend_try']))+cost>Decimal(str(cfg['monthly_try'])):raise Denied('monthly_budget_limit','Topluluğun aylık model bütçesi doldu.',429)
        cid=str(uuid.uuid4())
        c.execute('UPDATE daily_usage SET reserved_tokens=reserved_tokens+%s,reserved_try=reserved_try+%s WHERE user_id=%s AND day=%s',(base,cost,uid,d))
        if grant:c.execute('UPDATE quota_grants SET reserved=reserved+%s WHERE id=%s',(extra,grant['id']))
        c.execute('INSERT INTO model_calls(id,job_id,user_id,day,month,model,provider,reserved_tokens,reserved_try,quota_reserved,grant_id,grant_reserved,price,fx) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(cid,job_id,uid,d,m,model,provider,amount,cost,base,grant['id'] if grant else None,extra,dumps(price),fx))
        c.execute('UPDATE jobs SET call_count=call_count+1,heartbeat_at=now() WHERE id=%s',(job_id,))
        return cid

def normalize_usage(u,provider='openai'):
    # OpenAI input includes cached tokens. Gemini output excludes thoughts.
    if 'promptTokenCount' in u:
        i=int(u.get('promptTokenCount',0));o=int(u.get('candidatesTokenCount',0))+int(u.get('thoughtsTokenCount',0));cache=int(u.get('cachedContentTokenCount',0));reason=int(u.get('thoughtsTokenCount',0))
    else:
        i=int(u.get('prompt_tokens',u.get('input_tokens',0)));o=int(u.get('completion_tokens',u.get('output_tokens',0)))
        details=u.get('prompt_tokens_details') or u.get('input_tokens_details') or {}
        cache=int(details.get('cached_tokens',u.get('cached_tokens',0)) or 0)
        reason=int((u.get('completion_tokens_details') or u.get('output_tokens_details') or {}).get('reasoning_tokens',0) or 0)
    if min(i,o,cache,reason)<0:raise ValueError('negative usage')
    return {'input':i,'output':o,'cached':min(cache,i),'reasoning':reason}

def settle(call_id,usage=None,error=None,definitely_unbilled=False):
    with connect() as c:
        c.execute('SELECT pg_advisory_xact_lock(761904)')
        r=c.execute('SELECT * FROM model_calls WHERE id=%s FOR UPDATE',(call_id,)).fetchone()
        if not r or r['status']!='reserved':return
        # Unknown provider outcomes remain charged at the reserved ceiling, never silently refunded.
        if usage:
            i,o,cache,reason=(usage[k] for k in ['input','output','cached','reasoning']);tokens=i+o;p=r['price']
            cost=(Decimal(i-cache)*Decimal(str(p['input']))+Decimal(cache)*Decimal(str(p.get('cached',p['input'])))+Decimal(o)*Decimal(str(p['output'])))*r['fx']/Decimal(1_000_000);status='settled'
        elif definitely_unbilled:i=o=cache=reason=tokens=0;cost=Decimal(0);status='unbilled'
        else:i=r['reserved_tokens'];o=cache=reason=0;tokens=r['reserved_tokens'];cost=r['reserved_try'];status='estimated'
        base=min(tokens,r['quota_reserved']);extra=min(max(0,tokens-base),r['grant_reserved']);overflow=max(0,tokens-base-extra);base+=overflow
        c.execute('UPDATE daily_usage SET reserved_tokens=reserved_tokens-%s,tokens=tokens+%s,reserved_try=reserved_try-%s,cost_try=cost_try+%s WHERE user_id=%s AND day=%s',(r['quota_reserved'],base,r['reserved_try'],cost,r['user_id'],r['day']))
        if r['grant_id']:c.execute('UPDATE quota_grants SET reserved=reserved-%s,used=used+%s WHERE id=%s',(r['grant_reserved'],extra,r['grant_id']))
        c.execute('UPDATE model_calls SET status=%s,input_tokens=%s,output_tokens=%s,cached_tokens=%s,reasoning_tokens=%s,cost_try=%s,error_code=%s,finished_at=now() WHERE id=%s',(status,i,o,cache,reason,cost,error,call_id))
        if overflow or cost>r['reserved_try']:
            # Provider usage can exceed a conservative preflight estimate because of
            # hidden reasoning tokens. Record the variance without taking the whole
            # platform offline; normal daily/monthly budget gates still apply to every
            # new reservation.
            audit(c,r['user_id'],'reservation.overrun',call_id,{'token_overrun':overflow,'cost_overrun_try':float(max(Decimal(0),cost-r['reserved_try']))})

"""Scoped archive, opt-in document sharing, and quiet share conversations."""
import re, uuid
from common import *

TG_CONSENT = '2026-09-20-telegram-v1'
KINDS = {'general','course','exam','note','idea','reminder','project','meeting'}

def infer_metadata(name, text='', hint=''):
    sample=' '.join((str(hint),str(name),str(text)[:3000])).replace('_',' ')
    year=re.search(r'\b(20\d{2})\s*[-–/]\s*(20\d{2})\b',sample)
    grade=re.search(r'\b([1-6])\s*\.?\s*s[ıi]n[ıi]f\b',sample,re.I)
    term=re.search(r'\b([1-3])\s*\.?\s*d[oö]nem\b',sample,re.I)
    exam=re.search(r'\b(vize|final|b[üu]t[üu]nleme|quiz|ara\s*s[ıi]nav|k[ıi]sa\s*s[ıi]nav)\b',sample,re.I)
    course=(re.search(r'\b20\d{2}\s*[-–/]\s*20\d{2}\s+([\wÇĞİÖŞÜçğıöşü +.-]{1,70}?)\s+ders(?:i|inin)?\b',sample,re.I)
            or re.search(r'\bd[oö]nem\s+([\wÇĞİÖŞÜçğıöşü +.-]{1,70}?)\s+ders(?:i|inin)?\b',sample,re.I)
            or re.search(r'\b([\wÇĞİÖŞÜçğıöşü+.-]{1,70})\s+ders(?:i|inin)?\b',sample,re.I))
    course_name=course.group(1).strip(' -.,') if course else ''
    if len(course_name)>70:course_name=course_name[-70:]
    meta={'academic_year':f'{year.group(1)}-{year.group(2)}' if year and int(year.group(2))==int(year.group(1))+1 else None,
          'grade':int(grade.group(1)) if grade else None,'semester':int(term.group(1)) if term else None,
          'course':course_name or None,'exam_type':exam.group(1).casefold() if exam else None}
    return {key:value for key,value in meta.items() if value is not None}

def validated_metadata(value):
    if value in (None,{}):return {}
    if not isinstance(value,dict):raise Denied('archive_metadata_invalid','Arşiv bilgisi geçersiz.',422)
    allowed={'academic_year','grade','semester','course','exam_type','tags','due_at','status'}
    if set(value)-allowed:raise Denied('archive_metadata_invalid','Bilinmeyen arşiv alanı.',422)
    result={}
    for key in ('academic_year','course','exam_type'):
        if key in value:
            text=str(value[key]).strip()
            if len(text)>100:raise Denied('archive_metadata_invalid','Arşiv alanı çok uzun.',422)
            if text:result[key]=text
    if 'academic_year' in result and not re.fullmatch(r'20\d{2}-20\d{2}',result['academic_year']):raise Denied('archive_metadata_invalid','Yıl 2025-2026 biçiminde olmalı.',422)
    for key,maximum in [('grade',6),('semester',3)]:
        if key in value and value[key] not in (None,''):
            try:number=int(value[key])
            except (TypeError,ValueError):raise Denied('archive_metadata_invalid','Sınıf veya dönem geçersiz.',422)
            if not 1<=number<=maximum:raise Denied('archive_metadata_invalid','Sınıf veya dönem geçersiz.',422)
            result[key]=number
    if 'tags' in value:
        if not isinstance(value['tags'],list) or len(value['tags'])>12:raise Denied('archive_metadata_invalid','En fazla 12 etiket kullanılabilir.',422)
        result['tags']=[str(tag).strip()[:40] for tag in value['tags'] if str(tag).strip()]
    if 'status' in value:
        status=str(value['status']).strip()
        if status not in {'active','done','archived'}:raise Denied('archive_metadata_invalid','Not durumu geçersiz.',422)
        result['status']=status
    if 'due_at' in value and value['due_at'] not in (None,''):
        try:
            due=datetime.fromisoformat(str(value['due_at']).replace('Z','+00:00'))
            if due.tzinfo is None:due=due.replace(tzinfo=TZ)
            result['due_at']=due.isoformat()
        except ValueError:raise Denied('archive_metadata_invalid','Hatırlatma tarihi geçersiz.',422)
    return result

def classify_document(name,text='',hint='',kind=None,metadata=None):
    meta={**infer_metadata(name,text,hint),**validated_metadata(metadata)}
    selected=kind or ('exam' if meta.get('exam_type') else 'course' if meta.get('course') else 'general')
    if selected not in KINDS:raise Denied('archive_kind_invalid','Arşiv türü geçersiz.',422)
    review='ready' if selected not in {'exam','course'} or (meta.get('academic_year') and meta.get('course') and (selected!='exam' or meta.get('exam_type'))) else 'needs_review'
    return selected,meta,review

def create_memory(c,u,title,text,kind='note',metadata=None,scope='personal'):
    from portal import add_document
    if kind not in {'note','idea','reminder'}:raise Denied('memory_kind_invalid','Not türü geçersiz.',422)
    if scope=='board':board(u)
    metadata=validated_metadata(metadata)
    did=add_document(c,u,str(title).strip()[:180],str(text).strip()[:50000],scope=scope,kind=kind,metadata=metadata)
    due=metadata.get('due_at')
    if kind=='reminder' and due:
        c.execute('INSERT INTO memory_reminders(document_id,user_id,due_at) VALUES(%s,%s,%s) ON CONFLICT(document_id) DO UPDATE SET due_at=excluded.due_at,sent_at=NULL',(did,u['id'],due))
    return did

def visible_clause(uid,board_user):
    return "(d.owner_id=%s OR (d.scope='community' AND NOT d.google_source) OR (d.scope='board' AND %s) OR (d.review_status='submitted' AND %s) OR EXISTS(SELECT 1 FROM document_shares s WHERE s.document_id=d.id AND s.to_user=%s AND s.status='accepted')) AND (NOT d.google_source OR %s)"

def list_documents(c,u,query='',filters=None,limit=50,offset=0):
    filters=filters or {};query=str(query).strip()[:120];limit=max(1,min(int(limit),100));offset=max(0,min(int(offset),100000))
    sql="SELECT d.id,d.owner_id,d.name,d.scope,d.google_source,d.filename,d.mime,d.created_at,d.archive_kind,d.archive_meta,d.review_status FROM documents d WHERE "+visible_clause(u['id'],u['role'] in BOARD)
    params=[u['id'],u['role'] in BOARD,u['role'] in BOARD,u['id'],u['role'] in BOARD]
    if query:
        sql+=" AND (d.name ILIKE %s OR EXISTS(SELECT 1 FROM document_chunks ch WHERE ch.document_id=d.id AND ch.search_index@@plainto_tsquery('simple',%s)))"
        params.extend(['%'+query+'%',query.casefold()])
    if filters.get('scope') in {'personal','community','board','owner'}:sql+=' AND d.scope=%s';params.append(filters['scope'])
    if filters.get('kind') in KINDS:sql+=' AND d.archive_kind=%s';params.append(filters['kind'])
    for key in ('academic_year','course','semester','grade','exam_type'):
        if filters.get(key) not in (None,''):
            sql+=' AND d.archive_meta->>%s=%s';params.extend([key,str(filters[key])])
    sql+=' ORDER BY d.created_at DESC,d.id DESC LIMIT %s OFFSET %s';params.extend([limit+1,offset])
    rows=c.execute(sql,params).fetchall()
    return {'items':rows[:limit],'next_offset':offset+limit if len(rows)>limit else None}

def create_share(c,u,document_id,recipient,detail=''):
    d=c.execute('SELECT id,name,scope,google_source,owner_id FROM documents WHERE id=%s',(document_id,)).fetchone()
    if not d or d['owner_id']!=u['id'] or d['scope']!='personal' or d['google_source']:raise Denied('share_denied','Yalnız size ait kişisel belge veya notu paylaşabilirsiniz.',403)
    target=c.execute('SELECT id,name,email,status FROM platform_users WHERE lower(email)=lower(%s) OR id::text=%s',(str(recipient),str(recipient))).fetchone()
    if not target or target['status']!='active' or target['id']==u['id']:raise Denied('share_recipient_invalid','Aktif, farklı bir kullanıcı seçin.',422)
    detail=str(detail).strip()[:1000]
    existing=c.execute('SELECT id,status FROM document_shares WHERE document_id=%s AND to_user=%s FOR UPDATE',(d['id'],target['id'])).fetchone()
    if existing:
        if existing['status']=='accepted':return {'id':str(existing['id']),'status':'accepted','recipient':target['name']}
        c.execute("UPDATE document_shares SET status='pending',introduction=%s,updated_at=now() WHERE id=%s",(encrypt(detail) if detail else None,existing['id']));sid=str(existing['id'])
    else:
        sid=str(uuid.uuid4());c.execute('INSERT INTO document_shares(id,document_id,from_user,to_user,introduction) VALUES(%s,%s,%s,%s,%s)',(sid,d['id'],u['id'],target['id'],encrypt(detail) if detail else None))
    c.execute('DELETE FROM document_acl WHERE document_id=%s AND user_id=%s',(d['id'],target['id']))
    audit(c,u['id'],'document.share_requested',str(d['id']),{'recipient_id':target['id']})
    return {'id':sid,'status':'pending','recipient':target['name'],'document':d['name']}

def share_action(c,u,share_id,action):
    row=c.execute('SELECT * FROM document_shares WHERE id=%s FOR UPDATE',(share_id,)).fetchone()
    if not row:raise Denied('share_missing','Paylaşım bulunamadı.',404)
    if action in {'accept','decline'} and row['to_user']!=u['id']:raise Denied('share_denied','Bu davet size ait değil.')
    if action=='revoke' and row['from_user']!=u['id']:raise Denied('share_denied','Bu paylaşım size ait değil.')
    if action not in {'accept','decline','revoke'}:raise Denied('share_action_invalid','Paylaşım işlemi geçersiz.',422)
    if action!='revoke' and row['status']!='pending':raise Denied('share_not_pending','Paylaşım daveti artık beklemiyor.',409)
    status={'accept':'accepted','decline':'declined','revoke':'revoked'}[action]
    c.execute('UPDATE document_shares SET status=%s,updated_at=now() WHERE id=%s',(status,row['id']))
    if action=='accept':c.execute('INSERT INTO document_acl(document_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(row['document_id'],row['to_user']))
    else:c.execute('DELETE FROM document_acl WHERE document_id=%s AND user_id=%s',(row['document_id'],row['to_user']))
    audit(c,u['id'],'document.share_'+action,str(row['document_id']),{'share_id':str(row['id'])})
    return {'ok':True,'status':status}

def list_shares(c,u):
    rows=c.execute("""SELECT s.id,s.document_id,s.from_user,s.to_user,s.status,s.introduction,s.created_at,d.name,d.archive_meta,
       sender.name from_name,recipient.name to_name
       FROM document_shares s JOIN documents d ON d.id=s.document_id
       JOIN platform_users sender ON sender.id=s.from_user JOIN platform_users recipient ON recipient.id=s.to_user
       WHERE s.to_user=%s OR s.from_user=%s ORDER BY s.updated_at DESC LIMIT 200""",(u['id'],u['id'])).fetchall()
    for row in rows:row['introduction']=decrypt(row['introduction']) if row['introduction'] else ''
    return {'items':rows}

def list_messages(c,u,share_id):
    share=c.execute('SELECT * FROM document_shares WHERE id=%s',(share_id,)).fetchone()
    if not share or u['id'] not in (share['from_user'],share['to_user']) or share['status']!='accepted':raise Denied('share_denied','Bu paylaşım konuşmasına erişiminiz yok.')
    rows=c.execute('SELECT id,sender_id,body,created_at,read_at FROM share_messages WHERE share_id=%s ORDER BY id DESC LIMIT 100',(share_id,)).fetchall()
    for row in rows:row['body']=decrypt(row['body'])
    c.execute('UPDATE share_messages SET read_at=now() WHERE share_id=%s AND sender_id<>%s AND read_at IS NULL',(share_id,u['id']))
    return {'items':list(reversed(rows))}

def send_message(c,u,share_id,text):
    share=c.execute('SELECT * FROM document_shares WHERE id=%s',(share_id,)).fetchone()
    if not share or u['id'] not in (share['from_user'],share['to_user']) or share['status']!='accepted':raise Denied('share_denied','Bu paylaşım konuşmasına erişiminiz yok.')
    text=str(text).strip()
    if not text or len(text)>4000:raise Denied('message_invalid','Mesaj 1–4000 karakter olmalı.',422)
    row=c.execute('INSERT INTO share_messages(share_id,sender_id,body) VALUES(%s,%s,%s) RETURNING id',(share_id,u['id'],encrypt(text))).fetchone()
    audit(c,u['id'],'share.message',str(share_id),{'message_id':row['id']})
    return {'id':row['id'],'ok':True}

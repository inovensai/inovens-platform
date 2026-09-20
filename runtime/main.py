import threading,time,subprocess,shutil,os
from common import *
import bridge,runner,telegram_bot,incident_center
from quota import settle
from tool_broker import execute_approvals,send_pending_invitation

def maintenance():
    send_due_operation_reminders()
    send_due_memory_reminders()
    with connect() as c:
        c.execute("UPDATE action_approvals SET status='expired' WHERE status IN ('pending','approved') AND expires_at<now()")
        stale=c.execute("SELECT id FROM model_calls WHERE status='reserved' AND created_at<now()-interval '5 minutes'").fetchall()
        # Retention uses database timestamps; deleting conversations preserves billing records.
        c.execute("DELETE FROM conversations WHERE updated_at<now()-interval '30 days' AND NOT EXISTS(SELECT 1 FROM jobs WHERE conversation_id=conversations.id AND status IN ('queued','running'))")
        c.execute("DELETE FROM accepted_commands WHERE created_at<now()-interval '1 day'")
        c.execute("DELETE FROM notifications WHERE created_at<now()-interval '1 day'")
        c.execute("DELETE FROM action_approvals WHERE created_at<now()-interval '30 days'")
        c.execute("DELETE FROM model_calls WHERE created_at<now()-interval '90 days' AND status<>'reserved'")
        c.execute("DELETE FROM daily_usage WHERE day<CURRENT_DATE-90 AND reserved_tokens=0 AND reserved_try=0")
        c.execute("DELETE FROM audit_events WHERE created_at<now()-interval '180 days'")
        c.execute("DELETE FROM job_recovery WHERE expires_at<now()")
        c.execute("DELETE FROM repair_runs WHERE created_at<now()-interval '180 days'")
        tombstones=c.execute("SELECT user_id,kind,target,deleted_at FROM deletion_tombstones WHERE kind='all' AND deleted_at>now()-interval '2 days'").fetchall()
    with connect() as c:profiles=c.execute('SELECT id,workspace_epoch FROM platform_users').fetchall()
    for p in profiles:
        parent=ROOT/'users'/str(p['id'])/'personal-worker'
        if parent.exists():
            with connect() as c:
                idle=c.execute("SELECT NOT EXISTS(SELECT 1 FROM jobs WHERE user_id=%s AND (status IN ('queued','running') OR finished_at>now()-interval '15 minutes')) idle",(p['id'],)).fetchone()['idle']
            if idle:subprocess.run(['docker','stop','-t','2','inovens-user-'+str(p['id'])],capture_output=True,timeout=10)
            for folder in parent.iterdir():
                if folder.is_dir() and folder.name.isdigit() and folder.name!=str(p['workspace_epoch']):shutil.rmtree(folder,ignore_errors=True)
            for f in parent.rglob('*.jsonl'):
                if f.stat().st_mtime<time.time()-30*86400:f.unlink(missing_ok=True)
    for row in stale:settle(row['id'],error='interrupted_unknown')
    for t in tombstones:
        home=ROOT/'users'/str(t['user_id']);marker=home/'last-deletion'
        stamp=t['deleted_at'].isoformat()
        if marker.exists() and marker.read_text()==stamp:continue
        subprocess.run(['docker','stop','-t','3','inovens-user-'+str(t['user_id'])],capture_output=True,timeout=10)
        if home.exists():shutil.rmtree(home)
        home.mkdir(parents=True,exist_ok=True);marker.write_text(stamp)

def send_due_operation_reminders():
    """Queue one reminder per due date and user, only while access remains valid."""
    from datetime import timedelta
    today=day_key()
    with connect() as c:
        rows=c.execute("""SELECT o.id,o.title,o.due_date,o.scope,o.assignee_user_id,
                   u.role,u.telegram_id,u.consent_version
              FROM operations o JOIN platform_users u ON u.id=o.assignee_user_id
              WHERE o.status IN ('candidate','open','running','paused')
                AND o.due_date IN (%s,%s) AND u.status='active' AND u.telegram_id IS NOT NULL
                AND (o.scope='community' OR (o.scope='board' AND u.role=ANY(%s))
                     OR (o.scope='owner' AND u.role='owner'))
              ORDER BY o.due_date,o.id LIMIT 200""",(today,today+timedelta(days=1),list(BOARD))).fetchall()
        for row in rows:
            if row['consent_version']!=CONSENT:continue
            kind='today' if row['due_date']==today else 'tomorrow'
            inserted=c.execute("""INSERT INTO operation_reminders(operation_id,user_id,due_date,kind)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING operation_id""",
                (row['id'],row['assignee_user_id'],row['due_date'],kind)).fetchone()
            if inserted:
                label='bugün' if kind=='today' else 'yarın'
                message=f"Topluluk işi hatırlatması: {row['title']} — son tarih {label} ({row['due_date'].isoformat()}). Durumu panelde Topluluk işleri bölümünden güncelleyebilirsiniz."
                c.execute('INSERT INTO notifications(user_id,body) VALUES(%s,%s)',(row['assignee_user_id'],encrypt(message)))

def send_due_memory_reminders():
    """Deliver each personal reminder once; content remains encrypted in storage."""
    with connect() as c:
        rows=c.execute("""SELECT r.document_id,r.user_id,d.name,d.body FROM memory_reminders r
          JOIN documents d ON d.id=r.document_id JOIN platform_users u ON u.id=r.user_id
          WHERE r.sent_at IS NULL AND r.due_at<=now() AND u.status='active'
          ORDER BY r.due_at LIMIT 100 FOR UPDATE OF r SKIP LOCKED""").fetchall()
        for row in rows:
            content=decrypt(row['body']).get('text','')
            message='⏰ Hatırlatma: '+row['name']+('\n'+content if content and content!=row['name'] else '')
            c.execute('INSERT INTO notifications(user_id,body) VALUES(%s,%s)',(row['user_id'],encrypt(message[:3800])))
            c.execute('UPDATE memory_reminders SET sent_at=now() WHERE document_id=%s',(row['document_id'],))
            audit(c,row['user_id'],'memory.reminder_queued',str(row['document_id']))

def loop():
    with connect() as c:
        jobs=c.execute("UPDATE jobs SET status='interrupted',error_code='service_restarted',finished_at=now() WHERE status='running' RETURNING id,user_id,channel,model,prompt").fetchall()
        for j in jobs:
            c.execute("INSERT INTO job_recovery(job_id,prompt) VALUES(%s,%s) ON CONFLICT(job_id) DO UPDATE SET prompt=excluded.prompt,expires_at=now()+interval '24 hours'",(j['id'],j['prompt']))
            incident_center.record('jobs','service_restarted','Servis yeniden başlarken çalışan iş kesildi.',{'channel':j['channel'],'model':j['model']},'error',j['id'],c)
        c.execute("UPDATE action_approvals SET status='uncertain' WHERE status='executing'")
    for j in jobs:subprocess.run(['docker','stop','-t','3','inovens-user-'+str(j['user_id'])],capture_output=True)
    threading.Thread(target=bridge.loop,daemon=True).start()
    if os.environ.get('TELEGRAM_ENABLED')=='1':
        threading.Thread(target=telegram_bot.loop,daemon=True).start()
        for _ in range(4):threading.Thread(target=telegram_bot.delivery_loop,daemon=True).start()
        threading.Thread(target=telegram_bot.typing_loop,daemon=True).start()
    threading.Thread(target=approval_loop,daemon=True).start()
    threading.Thread(target=maintenance_loop,daemon=True).start()
    threading.Thread(target=incident_center.loop,daemon=True).start()
    while True:
        try:
            runner.tick()
        except Exception as e:
            print('platform loop error',type(e).__name__,flush=True);incident_center.record('platform','platform_loop_error','Ana iş döngüsünde hata oluştu.',{'exception':type(e).__name__},'critical')
        time.sleep(.1)

def approval_loop():
    while True:
        try:execute_approvals();send_pending_invitation()
        except Exception as exc:
            print('approval loop error',type(exc).__name__,flush=True);incident_center.record('tools','approval_loop_error','Onay veya davet işlemi tamamlanamadı.',{'exception':type(exc).__name__})
        time.sleep(.5)

def maintenance_loop():
    while True:
        try:maintenance()
        except Exception as exc:
            print('maintenance loop error',type(exc).__name__,flush=True);incident_center.record('platform','maintenance_loop_error','Bakım işlemi tamamlanamadı.',{'exception':type(exc).__name__})
        time.sleep(60)

if __name__=='__main__':loop()

"""Independent router databases: no shared AI-Ege settings or OAuth accounts."""
import sqlite3,shutil,json,uuid,os
from common import *
SOURCE=Path('/home/ege/.9router/db/data.sqlite')

def configure(kind):
    dest=ROOT/'routers'/kind/'db/data.sqlite';first=not dest.exists()
    if first:
        src=sqlite3.connect(SOURCE);dst=sqlite3.connect(dest);src.backup(dst);dst.close();src.close();dest.chmod(0o600)
    c=sqlite3.connect(dest);stamp=now().isoformat()
    if first:
        for table in ['usageHistory','usageDaily','requestDetails','kv','proxyPools','apiKeys','combos','providerConnections','providerNodes']:
            try:c.execute('DELETE FROM '+table)
            except sqlite3.OperationalError:pass
        row=c.execute('SELECT id,data FROM settings LIMIT 1').fetchone();settings=json.loads(row[1]) if row else {}
        settings.update(codexAutoPing={'enabled':False},providerStrategies={},requireApiKey=True,requireLogin=True,requestDetailsEnabled=False,usageHistoryEnabled=False,capacityAdapter={'enabled':False})
        if row:c.execute('UPDATE settings SET data=? WHERE id=?',(json.dumps(settings),row[0]))
        c.execute('INSERT INTO apiKeys(id,key,name,machineId,isActive,createdAt) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),os.environ['ROUTER_KEY'],'Platform internal only',None,1,stamp))
    if kind=='general':
        src=sqlite3.connect(SOURCE)
        node=src.execute("SELECT * FROM providerNodes WHERE name='Meta'").fetchone()
        if not node:raise RuntimeError('Meta route missing')
        conn=src.execute('SELECT * FROM providerConnections WHERE provider=? AND isActive=1 LIMIT 1',(node[0],)).fetchone()
        if not conn:raise RuntimeError('Meta credential missing')
        c.execute('INSERT OR REPLACE INTO providerNodes VALUES(?,?,?,?,?,?)',node);c.execute('INSERT OR REPLACE INTO providerConnections VALUES(?,?,?,?,?,?,?,?,?,?)',conn)
        # Antigravity is a built-in provider, so it has connection rows without a
        # providerNodes row. Copy only its active OAuth connections into INOVENS's
        # isolated router database; other AI-Ege providers remain separate.
        for ag in src.execute("SELECT * FROM providerConnections WHERE provider='antigravity' AND isActive=1"):
            c.execute('INSERT OR REPLACE INTO providerConnections VALUES(?,?,?,?,?,?,?,?,?,?)',ag)
        for ocg in src.execute("SELECT * FROM providerConnections WHERE provider='opencode-go' AND isActive=1"):
            c.execute('INSERT OR REPLACE INTO providerConnections VALUES(?,?,?,?,?,?,?,?,?,?)',ocg)
        fixer=src.execute("SELECT * FROM combos WHERE name='inovens-panel-fixer'").fetchone()
        if fixer:c.execute('INSERT OR REPLACE INTO combos VALUES(?,?,?,?,?,?)',fixer)
        model=['ag/gemini-3.8-flash-medium',json.loads(node[3])['prefix']+'/muse-spark-1.3-contributor'];src.close()
    else:
        with connect() as p:r=p.execute("SELECT value FROM settings WHERE key='google_provider'").fetchone()
        if not r:c.commit();c.close();return
        nodeid='openai-compatible-chat-inovens-google-paid';key=decrypt(r['value']['key'])
        c.execute('INSERT OR REPLACE INTO providerNodes VALUES(?,?,?,?,?,?)',(nodeid,'openai-compatible','Google Paid',json.dumps({'prefix':'googlepaid','apiType':'chat','baseUrl':'https://generativelanguage.googleapis.com/v1beta/openai'}),stamp,stamp))
        c.execute('INSERT OR REPLACE INTO providerConnections VALUES(?,?,?,?,?,?,?,?,?,?)',('inovens-google-paid',nodeid,'apikey','Google Paid',None,1,1,json.dumps({'apiKey':key}),stamp,stamp));model=['googlepaid/gemini-3.5-flash-lite']
    for name in MODELS:
        c.execute('INSERT OR REPLACE INTO combos VALUES(?,?,?,?,?,?)',(name,name,'fallback',json.dumps(model),stamp,stamp))
    c.commit();c.execute('VACUUM');c.close()

if __name__=='__main__':
    configure('general');configure('google');print('Independent router databases configured.')

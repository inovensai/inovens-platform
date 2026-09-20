import json,shutil,socket,urllib.request,xml.etree.ElementTree as ET
from common import *

def port_up(port):
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.5):return True
    except OSError:return False

def sync_status():
    """Read only folder-level Syncthing progress; never expose filenames or key."""
    config=Path.home()/'.local/state/syncthing/config.xml'
    if not config.is_file():config=Path.home()/'.config/syncthing/config.xml'
    try:
        gui=ET.parse(config).getroot().find('gui')
        address=gui.findtext('address').replace('0.0.0.0','127.0.0.1')
        key=gui.findtext('apikey')
        if not address.startswith(('127.0.0.1:','localhost:')):return {'available':False,'folders':[]}
        folders=[]
        for folder,label in [('personal-codex','Codex projeleri'),('personal-chatgpt','ChatGPT projeleri')]:
            request=urllib.request.Request(f'http://{address}/rest/db/status?folder={folder}',headers={'X-API-Key':key})
            with urllib.request.urlopen(request,timeout=2) as response:data=json.load(response)
            folders.append({'id':folder,'label':label,'state':str(data.get('state','unknown'))[:40],
                            'need_files':int(data.get('needFiles') or 0),'need_bytes':int(data.get('needBytes') or 0),
                            'errors':int(data.get('errors') or 0)})
        return {'available':True,'folders':folders}
    except Exception:return {'available':False,'folders':[]}

def snapshot(c):
    disk=shutil.disk_usage(ROOT)
    database=False
    try:c.execute('SELECT 1').fetchone();database=True
    except Exception:pass
    private_verified=False
    try:
        from data_policy import verify_private_route
        verify_private_route();private_verified=True
    except Exception:pass
    return {'services':{'database':database,'general_router':port_up(20129)},'sync':sync_status(),'disk':{'total':disk.total,'free':disk.free},'jobs':c.execute('SELECT status,count(*) count FROM jobs GROUP BY status').fetchall(),'models':MODELS,'general_provider':'Öncelik: ag/gemini-3.8-flash-medium; genel içerikte fallback: Muse Spark 1.3 Contributor','google_provider':'Genel içerik normal rotada; hassas veya belirsiz içerik yalnız ayrı Antigravity Gemini rotasında işlenir','training_policy':{'contributor_scope':'general_non_sensitive_only','classifier':'local_sensitive_router_v4','uncertain':'restricted_route','restricted_route_pinned':private_verified,'restricted_model':'inovens-combo-ozel','restricted_providers':['antigravity'],'provider_zero_retention_claimed':False,'fallback':'stop_if_route_tampered_or_credentials_detected','document_content_logged':False},'retention':{'conversations_days':30,'usage_days':90,'audit_days':180,'server_backups_days':7,'disaster_recovery_copy_days':30},'isolation':'Kullanıcı başına ayrı konteyner, ağ kapalı; araçlar kimlik denetimli aracıdan geçer.'}

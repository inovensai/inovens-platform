import socket,threading,select,subprocess,time,shutil
from concurrent.futures import ThreadPoolExecutor
from common import *
EXEC=ThreadPoolExecutor(max_workers=1);CELLS={};PW=None

def relay(server,path,stop):
 server.settimeout(1)
 def pipe(a):
  try:
   b=socket.socket(socket.AF_UNIX);b.connect(str(path))
   with a,b:
    while not stop.is_set():
     ready,_,_=select.select([a,b],[],[],1)
     for src in ready:
      data=src.recv(65536)
      if not data:return
      (b if src is a else a).sendall(data)
  except OSError:pass
 while not stop.is_set():
  try:a,_=server.accept();threading.Thread(target=pipe,args=(a,),daemon=True).start()
  except socket.timeout:pass
  except OSError:return

def close(uid):
 cell=CELLS.pop(uid,None)
 if not cell:return
 try:cell['browser'].close()
 except Exception:pass
 cell['stop'].set();cell['server'].close()
 subprocess.run(['docker','stop','-t','2','inovens-browser-'+str(uid)],capture_output=True,timeout=10)

def invoke(uid,name,a):
 global PW
 from playwright.sync_api import sync_playwright
 from tool_broker import public_url
 if name=='close':close(uid);return {}
 if name=='browser_open':public_url(str(a.get('url','')))
 if uid not in CELLS:
  if len(CELLS)>=2:raise Denied('browser_capacity','İki tarayıcı şu an kullanımda; biraz sonra tekrar deneyin.',429)
  if PW is None:PW=sync_playwright().start()
  ipc=ROOT/'users'/str(uid)/'browser-ipc';ipc.mkdir(parents=True,exist_ok=True);(ipc/'cdp.sock').unlink(missing_ok=True)
  args=['docker','run','-d','--rm','--name','inovens-browser-'+str(uid),'--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','800m','--cpus','.7','--pids-limit','384','--user',str(os.getuid())+':'+str(os.getgid()),'--tmpfs','/tmp:rw,nosuid,size=256m','-v',str(ipc)+':/ipc','-v',str(ROOT/'egress')+':/egress:ro','inovens-browser:2']
  subprocess.run(args,check=True,capture_output=True,timeout=20)
  server=socket.socket();server.bind(('127.0.0.1',0));server.listen();stop=threading.Event();threading.Thread(target=relay,args=(server,ipc/'cdp.sock',stop),daemon=True).start()
  browser=None
  for _ in range(25):
   try:browser=PW.chromium.connect_over_cdp('http://127.0.0.1:'+str(server.getsockname()[1]),timeout=1500);break
   except Exception:time.sleep(.5)
  if not browser:
   stop.set();server.close();subprocess.run(['docker','stop','inovens-browser-'+str(uid)],capture_output=True);raise Denied('browser_start_failed','Tarayıcı başlatılamadı.',503)
  CELLS[uid]={'browser':browser,'page':None,'server':server,'stop':stop}
  context=browser.new_context(service_workers='block',accept_downloads=False)
  def route(r):
   try:
    if r.request.method not in ['GET','HEAD']:r.abort();return
    public_url(r.request.url);r.continue_()
   except Exception:r.abort()
  context.route('**/*',route);context.route_web_socket('**/*',lambda ws:ws.close())
  page=context.new_page();page.set_default_timeout(15000)
  CELLS[uid]={'browser':browser,'page':page,'server':server,'stop':stop}
 page=CELLS[uid]['page']
 if name=='browser_open':page.goto(a['url'],wait_until='domcontentloaded',timeout=30000)
 elif name=='browser_click':
  # Research browser follows links only; form submissions and external writes are blocked.
  link=page.get_by_role('link',name=str(a.get('text','')),exact=True).first
  href=link.get_attribute('href')
  from urllib.parse import urljoin
  target=urljoin(page.url,href or '');public_url(target);page.goto(target,wait_until='domcontentloaded',timeout=30000)
 return {'url':page.url,'title':page.title(),'text':page.locator('body').inner_text()[:14000],'links':page.get_by_role('link').evaluate_all('(xs)=>xs.slice(0,60).map(x=>({text:x.innerText.slice(0,100),url:x.href}))'),'trust':'untrusted_source','mode':'read_only'}

def guarded(uid,name,a):
 try:return invoke(uid,name,a)
 except Exception:
  close(uid);raise
def call(uid,name,a):return EXEC.submit(guarded,uid,name,a).result(timeout=90)
def release(uid):EXEC.submit(invoke,uid,'close',{})

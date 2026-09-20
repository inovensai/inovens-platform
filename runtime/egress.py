"""Public-address-only proxy over a Unix socket mounted exclusively in browser cells."""
import socketserver,socket,select,urllib.parse,os
from common import ROOT
from tool_broker import public_url
class Handler(socketserver.StreamRequestHandler):
 def handle(self):
  try:
   first=self.rfile.readline(8192).decode('latin1').strip();method,target,version=first.split(' ',2);headers=[]
   for _ in range(100):
    line=self.rfile.readline(8192)
    if line in [b'\r\n',b'\n',b'']:break
    headers.append(line)
   if method=='CONNECT':
    host,port=target.rsplit(':',1)
    if port!='443':raise ValueError()
    p,ip=public_url('https://'+target);up=socket.create_connection((ip,443),timeout=20);self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');self.wfile.flush()
   else:
    if method not in ['GET','HEAD']:raise ValueError()
    p,ip=public_url(target)
    if p.scheme!='http':raise ValueError()
    up=socket.create_connection((ip,p.port or 80),timeout=20)
    request=(method+' '+urllib.parse.urlunsplit(('','',p.path or '/',p.query,''))+' HTTP/1.1\r\nHost: '+p.netloc+'\r\nConnection: close\r\n').encode()
    for h in headers:
     if h.lower().startswith((b'host:',b'connection:',b'proxy-',b'content-length:',b'transfer-encoding:')):continue
     request+=h
    up.sendall(request+b'\r\n')
   with up:
    self.connection.settimeout(30);up.settimeout(30)
    while True:
     ready,_,_=select.select([self.connection,up],[],[],30)
     if not ready:return
     for src in ready:
      part=src.recv(65536)
      if not part:return
      (up if src is self.connection else self.connection).sendall(part)
  except Exception as exc:
   print('egress failure',type(exc).__name__,flush=True)
   try:self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
   except Exception:pass
class Server(socketserver.ThreadingUnixStreamServer):daemon_threads=True
if __name__=='__main__':
 path=ROOT/'egress/egress.sock';path.parent.mkdir(mode=0o700,parents=True,exist_ok=True);path.unlink(missing_ok=True)
 with Server(str(path),Handler) as server:os.chmod(path,0o600);server.serve_forever()

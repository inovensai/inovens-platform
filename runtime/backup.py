"""Daily authenticated encrypted backup, seven-day retention; no plaintext backup left behind."""
import os,tarfile,tempfile,subprocess,secrets,time,hashlib
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from common import ROOT

def run():
 folder=ROOT/'backups';folder.mkdir(exist_ok=True);keyfile=ROOT/'backup.key'
 if not keyfile.exists():keyfile.write_bytes(secrets.token_bytes(32));keyfile.chmod(0o600)
 name=time.strftime('platform-%Y%m%d-%H%M%S');dest=folder/(name+'.enc');nonce=secrets.token_bytes(12)
 with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
  tmp=Path(tmp);dump=tmp/'platform.dump'
  with dump.open('wb') as f:subprocess.run(['docker','exec','inovens-platform-db','pg_dump','-U','inovens','-Fc','inovens'],stdout=f,check=True,timeout=120)
  with dump.open('rb') as f:subprocess.run(['docker','exec','-i','inovens-platform-db','pg_restore','--list'],stdin=f,stdout=subprocess.DEVNULL,check=True,timeout=30)
  archive=tmp/'backup.tar'
  with tarfile.open(archive,'w') as t:
   t.add(dump,arcname='platform.dump')
   for part in ['users','runtime.env','bridge.env']:
    t.add(ROOT/part,arcname=part,filter=lambda info: info if info.isfile() or info.isdir() else None)
  enc=Cipher(algorithms.AES(keyfile.read_bytes()),modes.GCM(nonce)).encryptor()
  with archive.open('rb') as src,dest.open('wb') as out:
   out.write(b'INOVENS2'+nonce)
   while chunk:=src.read(1024*1024):out.write(enc.update(chunk))
   out.write(enc.finalize());out.write(enc.tag)
  dest.chmod(0o600)
 for p in folder.glob('platform-*.enc'):
  if p.stat().st_mtime<time.time()-7*86400:p.unlink()
 print('Encrypted backup created and PostgreSQL archive validated.')
if __name__=='__main__':
 from pathlib import Path
 run()

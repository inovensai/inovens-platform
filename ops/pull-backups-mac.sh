#!/bin/zsh
set -euo pipefail
umask 077

private_root="$HOME/Library/Application Support/INOVENS Recovery"
backup_dir="$private_root/backups"
key_dir="$private_root/key"
ssh_key="$HOME/.ssh/inovens-recovery-ssh"
cpanel_token="$key_dir/cpanel.token"
remote="ege@ege-kasa:/home/ege/.local/share/inovens-platform"

mkdir -p "$backup_dir" "$key_dir"
chmod 700 "$private_root" "$backup_dir" "$key_dir"
rsync -azq --partial -e "ssh -i $ssh_key -o BatchMode=yes -o ConnectTimeout=15" \
  --include='platform-*.enc' --exclude='*' "$remote/backups/" "$backup_dir/"
rsync -azq -e "ssh -i $ssh_key -o BatchMode=yes -o ConnectTimeout=15" \
  "$remote/backup.key" "$key_dir/backup.key"
chmod 600 "$key_dir/backup.key" "$backup_dir"/platform-*.enc

/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 - "$backup_dir" "$key_dir/backup.key" <<'PY'
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import sys

backups = sorted(Path(sys.argv[1]).glob('platform-*.enc'))
if not backups:
    raise SystemExit('No copied backup found')
key = Path(sys.argv[2]).read_bytes()
if len(key) != 32:
    raise SystemExit('Invalid recovery key')
latest = backups[-1]
with latest.open('rb') as source:
    if source.read(8) != b'INOVENS2':
        raise SystemExit('Unknown backup format')
    nonce = source.read(12)
    source.seek(-16, 2)
    tag = source.read(16)
    source.seek(20)
    remaining = latest.stat().st_size - 36
    verifier = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            raise SystemExit('Truncated backup')
        remaining -= len(chunk)
        verifier.update(chunk)
    verifier.finalize()
print('Verified encrypted recovery copy:', latest.name)
PY

# Keep two alternating encrypted copies outside the home network. The AES key is
# deliberately never uploaded to cPanel; a partial upload cannot destroy both slots.
if [[ -s "$cpanel_token" ]]; then
  latest_file=$(ls -1t "$backup_dir"/platform-*.enc | head -1)
  slot=$((10#$(date +%d) % 2))
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 - "$latest_file" "$cpanel_token" "$slot" <<'PY'
from pathlib import Path
import mimetypes,requests,sys

source=Path(sys.argv[1]);token=Path(sys.argv[2]).read_text().strip();slot=sys.argv[3]
host='https://your-cpanel-host:2083'
headers={'Authorization':'cpanel ino3d2saicom:'+token}
mkdir=requests.get(host+'/json-api/cpanel',headers=headers,params={
 'cpanel_jsonapi_user':os.environ.get('CPANEL_USER','your-cpanel-user'),'cpanel_jsonapi_apiversion':'2','cpanel_jsonapi_module':'Fileman',
 'cpanel_jsonapi_func':'mkdir','path':'/home/your-cpanel-user','name':'inovens-backups','permissions':'0700'},timeout=30)
mkdir.raise_for_status()
name='platform-offsite-'+slot+'.enc'
with source.open('rb') as handle:
 response=requests.post(host+'/execute/Fileman/upload_files',headers=headers,
  data={'dir':'/home/your-cpanel-user/inovens-backups','overwrite':'1'},
  files={'file-1':(name,handle,'application/octet-stream')},timeout=600)
response.raise_for_status();payload=response.json()
status=payload.get('status',payload.get('result',{}).get('status'))
errors=payload.get('errors') or payload.get('result',{}).get('errors')
if status in (0,False) or errors:raise SystemExit('Offsite upload failed')
print('Uploaded encrypted offsite copy:',name,source.stat().st_size)
PY
fi

find "$backup_dir" -type f -name 'platform-*.enc' -mtime +30 -delete

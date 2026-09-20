"""Generate one verified image in an authorized community workspace."""
import json,re,subprocess,threading,uuid
from pathlib import Path
from common import ROOT,BOARD,Denied

OPENCLAW=Path('/home/ege/.npm-global/bin/openclaw')
MODEL='openai/gpt-image-2'
FORMATS={'png':'.png','jpeg':'.jpg','jpg':'.jpg','webp':'.webp'}
SIZES={'1024x1024','1536x1024','1024x1536'}
QUALITIES={'low','medium','high','auto'}
LOCK=threading.Lock()

def target_path(user,args):
    requested=str(args.get('outputFormat') or '').casefold().strip()
    raw_name=Path(str(args.get('filename') or 'inovens-gorsel.png')).name
    inferred=raw_name.rsplit('.',1)[-1].casefold() if '.' in raw_name else ''
    format_name=requested or (inferred if inferred in FORMATS else 'png')
    if format_name not in FORMATS:raise Denied('image_format','Desteklenen görsel biçimleri: PNG, JPEG ve WebP.',422)
    stem=Path(raw_name).stem if inferred in FORMATS else raw_name
    stem=re.sub(r'[^\w()\- ]','-',stem,flags=re.UNICODE).strip(' .-')[:140] or 'inovens-gorsel'
    base=ROOT/'users'/str(user['id'])/'personal-worker'/str(user.get('workspace_epoch',0))/'workspace'
    base.mkdir(parents=True,exist_ok=True)
    return base/(stem+FORMATS[format_name]),format_name

def create(user,job,args):
    if user.get('role') not in BOARD or job.get('scope')!='board':
        raise Denied('image_community_only','Görsel üretimi şimdilik yalnız yetkili kullanıcıların /topluluk alanında kullanılabilir.',403)
    prompt=str(args.get('prompt') or '').strip()
    if len(prompt)<8 or len(prompt)>6000:raise Denied('image_prompt','Görsel açıklaması 8–6000 karakter arasında olmalıdır.',422)
    size=str(args.get('size') or '1024x1024')
    quality=str(args.get('quality') or 'medium').casefold()
    if size not in SIZES:raise Denied('image_size','Desteklenen boyutlar: 1024x1024, 1536x1024 ve 1024x1536.',422)
    if quality not in QUALITIES:raise Denied('image_quality','Desteklenen kalite seçenekleri: low, medium, high ve auto.',422)
    if not OPENCLAW.is_file():raise Denied('image_service_unavailable','Görsel üretim servisi hazır değil.',503)
    target,format_name=target_path(user,args)
    temporary=target.with_name('.image-'+uuid.uuid4().hex+FORMATS[format_name])
    command=[str(OPENCLAW),'infer','image','generate','--agent','ai-ege','--model',MODEL,
             '--quality',quality,'--size',size,'--output-format','jpeg' if format_name in {'jpg','jpeg'} else format_name,
             '--output',str(temporary),'--prompt',prompt,'--timeout-ms','360000','--json']
    try:
        with LOCK:
            process=subprocess.run(command,capture_output=True,text=True,timeout=390,check=False)
        if process.returncode!=0:raise Denied('image_generation_failed','Görsel sağlayıcısı üretimi tamamlayamadı; iş kaydı korundu.',502)
        result=json.loads(process.stdout)
        output=(result.get('outputs') or [{}])[0]
        produced=Path(str(output.get('path') or temporary))
        if produced.resolve()!=temporary.resolve() or not produced.is_file() or produced.stat().st_size>25_000_000:
            raise Denied('image_generation_failed','Görsel çıktısı güvenli biçimde alınamadı.',502)
        from document_qa import validate_document
        qa=validate_document(produced,target.parent/'.qa')
        if not qa['ok']:raise Denied('image_quality_failed','Görsel üretildi ancak görsel kalite kontrolünden geçmedi.',422)
        produced.replace(target)
        return {'path':'/workspace/'+target.name,'filename':target.name,'format':format_name,
                'size':target.stat().st_size,'width':qa.get('width'),'height':qa.get('height'),
                'quality_check':'passed','verified':True,'provider':'openai','model':MODEL}
    except subprocess.TimeoutExpired:
        raise Denied('image_generation_timeout','Görsel üretimi süre sınırını aştı; iş kaydı korundu.',504)
    except json.JSONDecodeError:
        raise Denied('image_generation_failed','Görsel sağlayıcısı geçerli bir çıktı döndürmedi.',502)
    finally:
        temporary.unlink(missing_ok=True)

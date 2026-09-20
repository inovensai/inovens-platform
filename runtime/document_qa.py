"""Render generated documents before delivery and reject visibly broken output."""
from pathlib import Path
import hashlib, json, statistics, zipfile

def _preview_dir(path,root):
    digest=hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    target=Path(root)/(path.stem+'-'+digest);target.mkdir(parents=True,exist_ok=True)
    return target

def _pdf(path,root):
    import fitz
    from PIL import Image,ImageStat
    reasons=[];screens=[]
    try:document=fitz.open(path)
    except Exception:return {'ok':False,'reasons':['pdf_acilamadi'],'screenshots':[]}
    if document.needs_pass or not 1<=document.page_count<=200:return {'ok':False,'reasons':['pdf_sayfa_yapisi_gecersiz'],'screenshots':[]}
    page_count=document.page_count;target=_preview_dir(path,root);samples={0,page_count//2,page_count-1}
    for number,page in enumerate(document):
        rect=page.rect
        if rect.width<200 or rect.height<200:reasons.append(f'sayfa_{number+1}_boyutu_gecersiz')
        blocks=page.get_text('dict').get('blocks',[]);lines=[];font_sizes=[]
        for block in blocks:
            for line in block.get('lines',[]):
                line_text=''.join(span.get('text','') for span in line.get('spans',[])).strip()
                if line_text:lines.append(line)
                font_sizes.extend(float(span.get('size',0)) for span in line.get('spans',[]) if span.get('text','').strip())
        text=page.get_text().strip();words=page.get_text('words')
        if len(text)<8 and not page.get_images(full=True):reasons.append(f'sayfa_{number+1}_bos')
        if len(text)>200 and len(lines)<3:reasons.append(f'sayfa_{number+1}_tek_satira_sikismis')
        if words:
            vertical=max(w[3] for w in words)-min(w[1] for w in words)
            if len(text)>200 and vertical<rect.height*.08:reasons.append(f'sayfa_{number+1}_dikey_yayilim_yetersiz')
            overflow=sum(1 for w in words if w[0]<-2 or w[1]<-2 or w[2]>rect.width+2 or w[3]>rect.height+2)
            if overflow>max(2,len(words)//100):reasons.append(f'sayfa_{number+1}_icerik_tasiyor')
        if font_sizes and statistics.median(font_sizes)<7:reasons.append(f'sayfa_{number+1}_yazi_cok_kucuk')
        pix=page.get_pixmap(matrix=fitz.Matrix(1.25,1.25),alpha=False)
        image=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
        gray=image.convert('L');hist=gray.histogram();pixels=gray.width*gray.height
        ink=sum(hist[:245])/pixels
        if ink<.001:reasons.append(f'sayfa_{number+1}_goruntu_bos')
        if ImageStat.Stat(gray).stddev[0]<1:reasons.append(f'sayfa_{number+1}_goruntu_tek_renk')
        if number in samples:
            output=target/f'page-{number+1:03d}.png';image.save(output,optimize=True);screens.append(str(output))
    document.close()
    return {'ok':not reasons,'reasons':list(dict.fromkeys(reasons)),'screenshots':screens,'pages':page_count}

def _image(path,root):
    from PIL import Image,ImageStat
    try:
        with Image.open(path) as source:
            source.verify()
        with Image.open(path) as source:
            image=source.convert('RGB');width,height=image.size
            if width<200 or height<200:return {'ok':False,'reasons':['gorsel_cozunurlugu_yetersiz'],'screenshots':[]}
            thumb=image.copy();thumb.thumbnail((1600,1600));target=_preview_dir(path,root)/'preview.png';thumb.save(target,optimize=True)
            extrema=ImageStat.Stat(thumb.convert('L')).extrema[0]
            if extrema[1]-extrema[0]<3:return {'ok':False,'reasons':['gorsel_neredeyse_tek_renk'],'screenshots':[str(target)]}
            return {'ok':True,'reasons':[],'screenshots':[str(target)],'width':width,'height':height}
    except Exception:return {'ok':False,'reasons':['gorsel_acilamadi'],'screenshots':[]}

def validate_document(path,preview_root):
    path=Path(path);suffix=path.suffix.lower()
    if suffix=='.pdf':return _pdf(path,preview_root)
    if suffix in {'.png','.jpg','.jpeg','.webp','.gif'}:return _image(path,preview_root)
    if suffix in {'.docx','.xlsx','.pptx','.zip'}:
        try:
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:return {'ok':False,'reasons':['arsiv_bozuk'],'screenshots':[]}
                if not archive.namelist():return {'ok':False,'reasons':['arsiv_bos'],'screenshots':[]}
            return {'ok':True,'reasons':[],'screenshots':[],'structural_only':True}
        except Exception:return {'ok':False,'reasons':['belge_acilamadi'],'screenshots':[]}
    if suffix in {'.txt','.md','.csv','.json','.yaml','.yml'}:
        try:
            text=path.read_text(errors='replace')
            if suffix=='.json':json.loads(text)
            return {'ok':bool(text.strip()),'reasons':[] if text.strip() else ['metin_bos'],'screenshots':[],'structural_only':True}
        except Exception:return {'ok':False,'reasons':['metin_acilamadi'],'screenshots':[]}
    return {'ok':False,'reasons':['dosya_turu_denetlenemiyor'],'screenshots':[]}

if __name__=='__main__':
    import sys
    path=Path(sys.argv[1]).resolve();result=validate_document(path,path.parent/'.qa')
    print(json.dumps(result,ensure_ascii=False));raise SystemExit(0 if result['ok'] else 2)

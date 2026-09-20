"""Turn natural-language requests into server-verifiable completion conditions."""
import math,re
from pathlib import Path
from common import decrypt, day_key

FORMAT_PATTERNS={
    '.pdf':r'\bpdf\b',
    '.docx':r'\b(?:docx|word belgesi|word dosyası)\b',
    '.xlsx':r'\b(?:xlsx|excel|hesap tablosu)\b',
    '.pptx':r'\b(?:pptx|powerpoint|sunum)\b',
    '.csv':r'\bcsv\b',
    '.png':r'\bpng\b',
    '.jpg':r'\b(?:jpe?g)\b',
    '.webp':r'\bwebp\b',
}
FACT_NOUNS=r'(?:haber\w*|patent\w*|makale\w*|girişim\w*|şirket\w*|örnek\w*|fikir\w*|gelişme\w*|madde\w*|kategori\w*|kaynak\w*|ürün\w*)'
SOURCE_PATTERN=r'\b(?:haber\w*|güncel\w*|bugün\w*|dün\w*|son dakika|araştır\w*|incele\w*|doğrula\w*|kaynak\w*|gelişme\w*|gündem\w*|sonuç\w*|duyuru\w*|patent\w*|makale\w*)\b'
WRITING_PATTERN=r'\b(?:mail|e-?posta|taslak|metin)\w*\b.*\b(?:yaz\w*|oluştur\w*|hazırla\w*|düzenle\w*|değiştir\w*|tasarla\w*)\b|\b(?:yaz\w*|oluştur\w*|hazırla\w*|düzenle\w*|değiştir\w*|tasarla\w*)\b.*\b(?:mail|e-?posta|taslak|metin)\w*\b'
EXPLICIT_RESEARCH_PATTERN=r'\b(?:haber\w*|güncel\w*|bugün\w*|dün\w*|son dakika|araştır\w*|web\w*|internet\w*|incele\w*|doğrula\w*|gündem\w*|patent\w*|makale\w*)\b'
DATE_PATTERN=r'\b(?:20\d{2}|\d{1,2}[./-]\d{1,2}[./-]20\d{2}|\d{1,2}\s+(?:ocak|şubat|mart|nisan|mayıs|haziran|temmuz|ağustos|eylül|ekim|kasım|aralık))\b'
URL_PATTERN=r'https?://[^\s<>]+'
IMAGE_NOUN_PATTERN=r'\b(?:görsel\w*|gorsel\w*|resim\w*|afiş\w*|afis\w*|poster\w*|logo\w*|illüstrasyon\w*|illustrasyon\w*)\b'
IMAGE_ACTION_PATTERN=r'\b(?:oluştur\w*|olustur\w*|üret\w*|uret\w*|tasarla\w*|çiz\w*|ciz\w*|hazırla\w*|hazirla\w*)\b'

def requested_count(text):
    """Extract a result count without mistaking dates such as 2026 for it."""
    values=[]
    for match in re.finditer(r'(?<!\d)(\d{1,3})\s+(?='+FACT_NOUNS+r'\b)',str(text or '').casefold(),re.I):
        value=int(match.group(1))
        if 2<=value<=200:values.append(value)
    return max(values) if values else None

def infer(text,scope='personal'):
    lowered=str(text or '').casefold()
    extensions=[ext for ext,pattern in FORMAT_PATTERNS.items() if re.search(pattern,lowered,re.I)]
    source_required=bool(re.search(SOURCE_PATTERN,lowered,re.I));expected=requested_count(lowered)
    writing_request=bool(re.search(WRITING_PATTERN,lowered,re.I))
    source_opt_out=bool(re.search(r'kaynakça.{0,30}(?:belirtme|ekleme|olmasın|çıkar)|(?:belirtme|ekleme|olmasın|çıkar).{0,30}kaynakça',lowered,re.I))
    if source_opt_out or (writing_request and not re.search(EXPLICIT_RESEARCH_PATTERN,lowered,re.I)):source_required=False
    min_reads=(min(12,max(3,math.ceil(expected/4))) if expected else 3) if source_required else 0
    if source_required and re.search(URL_PATTERN,lowered,re.I):min_reads=1
    requirements=[]
    def require(reason,*tools):requirements.append({'reason':reason,'any_of':list(tools)})
    mail=bool(re.search(r'\b(?:mail|e-?posta)\w*\b',lowered,re.I));drive=bool(re.search(r'\bdrive\b',lowered,re.I))
    if mail and re.search(r'\b(?:gönder\w*|yolla\w*|ilet\w*|at)\b',lowered,re.I):require('gmail_send','gmail_send')
    elif mail and re.search(r'\b(?:ara\w*|bul\w*|bak\w*|oku\w*|özetle\w*|listele\w*|göster\w*)\b',lowered,re.I):require('gmail_search','gmail_search')
    if drive and re.search(r'\b(?:yükle\w*|aktar\w*|kaydet\w*)\b',lowered,re.I):require('drive_upload','drive_upload')
    elif drive and re.search(r'\b(?:indir\w*|çek\w*)\b',lowered,re.I):
        require('drive_search','drive_search');require('drive_download','drive_download')
    elif drive and re.search(r'\b(?:ara\w*|bul\w*|bak\w*|listele\w*|göster\w*)\b',lowered,re.I):require('drive_search','drive_search')
    if re.search(r'\b(?:takvim|calendar)\w*\b',lowered,re.I) and re.search(r'\b(?:yaklaşan\w*|sıradaki\w*|bak\w*|listele\w*|göster\w*|neler|ne var)\b',lowered,re.I):require('calendar_upcoming','calendar_upcoming')
    github=bool(re.search(r'\b(?:github|git hub|repo|repository|depo)\w*\b',lowered,re.I))
    if github:
        if re.search(r'\b(?:aç\w*|olustur\w*|oluştur\w*|yarat\w*|create)\b',lowered,re.I):require('github_write','github_write')
        elif re.search(r'\b(?:hangi|neler|ne var|listele\w*|göster\w*|bak\w*|durum|issue|pr|pull request|action)\b',lowered,re.I):require('github_read','github_read')
    if re.search(r'\b(?:arşiv\w*|kayıtlı not\w*|geçmiş karar\w*|tüzük\w*)\b',lowered,re.I) and re.search(r'\b(?:ara\w*|bul\w*|bak\w*|özetle\w*|anlat\w*|göster\w*|ne|nedir|neler)\b',lowered,re.I):require('knowledge_search','knowledge_search')
    image_requested=bool(re.search(IMAGE_NOUN_PATTERN,lowered,re.I) and re.search(IMAGE_ACTION_PATTERN,lowered,re.I))
    document_extensions={'.pdf','.docx','.md','.txt','.xlsx','.pptx','.csv'}
    if image_requested and scope=='board' and not set(extensions).intersection(document_extensions):
        if not extensions:extensions.append('.png')
        require('image_create','image_create')
    if set(extensions).intersection({'.pdf','.docx','.md','.txt'}):require('document_create','document_create')
    return {'version':'2026-09-08-v3','scope':scope,'image_requested':image_requested,'required_extensions':extensions,'source_required':source_required,
            'discovery_required':source_required and not bool(re.search(URL_PATTERN,lowered,re.I)),
            'explicit_date':bool(re.search(DATE_PATTERN,lowered,re.I)),'expected_item_count':expected,
            'min_source_reads':min_reads,'required_tools':requirements,'current_date':day_key().isoformat()}

def from_job(job):
    payload=decrypt(job['prompt'])
    return payload.get('execution_contract') or infer(payload.get('text',''))

def system_instruction(job):
    contract=from_job(job);rules=[f"Bugünün platform tarihi {contract['current_date']}."]
    if contract.get('image_requested'):
        if contract.get('scope')=='board':rules.append('Bu görev gerçek bir görsel üretimi istiyor. platform__image_create aracını mutlaka kullan; yer tutucu SVG/HTML veya yalnız metinsel tasvir üretme. İstenen amaç, kompozisyon, renk, görsel üslup ve görselde yer alacak metni açık bir üretim promptuna dönüştür. Araçtan dönen dosyayı MEDIA satırıyla teslim et.')
        else:rules.append('Görsel üretimi şimdilik yalnız /topluluk alanında açıktır. Bu konuşma kişisel alandaysa görsel üretilmiş gibi davranma; kullanıcıya /topluluk komutuyla geçmesini kısaca söyle.')
    if contract['source_required']:
        minimum=contract.get('min_source_reads',3)
        rules.append(f'Bu görev güncel, tarihli veya doğrulanabilir dış bilgi gerektiriyor. Kullanıcı tarih vermediyse platform tarihine göre en yeni erişilebilir sonuçları seç. Bilgi kesim tarihine dayanma ve erişimin kapalı olduğunu varsayma. Önce platform__web_search ile kaynakları bul; ardından en az {minimum} farklı sonucu platform__web_fetch veya platform__browser_open/browser_read ile açıp doğrula. İddiaları açtığın kaynaklarla destekle; kaynak URL ve tarihlerini belgeye ekle.')
    expected=contract.get('expected_item_count')
    if expected:
        rules.append(f'Kullanıcı {expected} kayıt istiyor. Belgede tam {expected} ayrı ve anlamlı kayıt bulunmalı; sayıyı yüzeysel dolgu ile tamamlama. Her kayıtta ad/başlık, kurum veya sahip, tarih/dönem, 2-3 cümlelik açıklama-analiz ve ilgili kaynak bulunmalı.')
    if contract['required_extensions']:
        formats=', '.join(contract['required_extensions'])
        rules.append(f'Kullanıcı gerçek bir teslim dosyası istiyor ({formats}). PDF, DOCX, Markdown veya TXT için platform__document_create; görsel için platform__image_create aracını mutlaka kullan. Diğer biçimlerde /workspace altında okunabilir ve içeriği dolu dosya üret. Uzun raporlarda yönetici özeti, yöntem/kapsam, tematik bölümler, sonuç ve kaynakça oluştur. Dosyayı gerçekten oluşturmadan hazır olduğunu söyleme. Son yanıtta her dosyayı tek başına MEDIA:/workspace/dosya.ext satırıyla teslim et.')
    if contract.get('required_tools'):
        names=[]
        for requirement in contract['required_tools']:names.extend('platform__'+name for name in requirement['any_of'])
        rules.append('Kullanıcı dış sistemde gerçek bir işlem, okuma veya dosya istiyor. Yalnız açıklama yazma; uygun araçları çağır. Bu görevde gerekli araçlar: '+', '.join(dict.fromkeys(names))+'.')
    feedback=decrypt(job['prompt']).get('execution_feedback')
    if feedback:rules.append('Önceki deneme sunucu doğrulamasından geçmedi: '+str(feedback)+'. Eksik adımı araç kullanarak tamamla; önceki metinsel iddiayı tekrarlama.')
    rules.append('Kullanıcının amacı yeterince açıksa gereksiz netleştirme soruları sorma. Yazım hatalarını bağlamdan düzelt. Bir araç başarısız olursa aynı çağrıyı körlemesine tekrarlama; başka kaynak veya uygun başka araçla devam et. Dosya teslim mesajını 3-6 kısa satırda tut: kapsam, önemli bulgu ve doğrulama durumu; iç sistem yolunu veya ham kaynak listesini yazma, ek soruyla bitirme.')
    return '\n'.join(rules)

def candidate_files(home,text,structured,extract_refs):
    base=(home/'workspace').resolve();found=[]
    for ref in extract_refs(text,structured):
        if not ref.startswith('/workspace/'):continue
        path=(base/ref[len('/workspace/'):]).resolve()
        try:path.relative_to(base)
        except ValueError:continue
        if path.is_file() and path.stat().st_size>0:found.append(path)
    return found

def source_urls(rows):
    urls=[]
    for row in rows:
        detail=row.get('detail')
        if not isinstance(detail,dict):continue
        value=detail.get('source_url')
        if isinstance(value,str) and value.startswith(('http://','https://')):urls.append(value)
    return set(urls)

def completion_issue(job,home,text,structured,extract_refs):
    contract=from_job(job);tools=set();rows=[]
    if contract['source_required'] or contract.get('required_tools'):
        from common import connect
        with connect() as c:rows=c.execute("SELECT detail FROM audit_events WHERE action='tool.used' AND target=%s",(str(job['id']),)).fetchall()
        tools={row['detail'].get('tool') for row in rows if isinstance(row.get('detail'),dict)}
        for requirement in contract.get('required_tools',[]):
            if not tools.intersection(requirement['any_of']):return 'required_tool_missing'
    if contract['source_required']:
        if contract['discovery_required'] and 'web_search' not in tools:return 'source_search_missing'
        if not tools.intersection({'web_fetch','browser_open','browser_read'}):return 'source_read_missing'
        if len(source_urls(rows))<contract.get('min_source_reads',3):return 'source_breadth_missing'
    required=set(contract['required_extensions'])
    if required:
        files=[p for p in candidate_files(home,text,structured,extract_refs) if p.suffix.casefold() in required]
        if not files:return 'deliverable_missing'
        from document_qa import validate_document
        if not any(validate_document(path,(home/'workspace'/'.qa'))['ok'] for path in files):return 'deliverable_quality_failed'
    return None

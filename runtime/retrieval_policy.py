"""No model call is needed to decide whether archived knowledge is requested."""
import re
from common import decrypt

def allows_archive(job):
    prompt=decrypt(job['prompt'])
    text=prompt.get('text','').casefold().replace('i\u0307','i')
    # Explicit negative instruction wins; a generic mention of INOVENS is insufficient.
    if re.search(r'(arşiv|arsiv|hafıza|hafiza|rag).{0,24}(arama|bakma|kullanma|gitme|tarama)',text):return False
    return bool(re.search(r'arşiv|arsiv|tüzük|tuzuk|yönetmelik|yonetmelik|tutanak|kayıtlı|kayitli|kaydettiğimiz|kaydetti[gğ]im|notlarım|notlarimiz|fikirlerim|fikirlerimiz|hatırlatmalarım|hatirlatmalarim|belgelerimiz|belgelerim|dosyalarım|dosyalarimiz|hafıza|hafiza|hatırla|hatirla|önceki konuş|onceki konus|geçmiş konuş|gecmis konus|daha önce.{0,40}(konuş|karar|plan|kaydet)|geçen.{0,25}(toplantı|karar)|toplantı.{0,20}karar|arşivden|\brag\b',text))

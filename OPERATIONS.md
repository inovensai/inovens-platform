# INOVENS'AI işletim notları

## Kaynak ve canlı ortam

- Kaynak kodu bu dizindeki Git deposunda tutulur.
- Sunucu çalışma dizini: `/home/ege/.local/share/inovens-platform/runtime`
- Web arayüzü `frontend` altında derlenir; üretilen `public` dosyaları cPanel'e aktarılır.
- Kimlik bilgileri, ortam dosyaları, yedek anahtarı ve derlenmiş varlıklar Git'e alınmaz.

## Yayın öncesi kapılar

1. Üretim veritabanının kopyası olan `inovens_test` üzerinde tüm `pytest` testleri geçmelidir.
2. `npm run build` hatasız tamamlanmalıdır.
3. Çalışan iş yoksa runtime dosyaları zaman damgalı geri dönüş dizinine kopyalanır.
4. Servisler yeniden başlatılır ve Unix soketindeki `/health` yanıtı doğrulanır.
5. Web varlıkları önce, `index.html` en son yüklenir. `CPANEL_TOKEN_FILE` özel token dosyasına işaret edecek biçimde `python3 ops/deploy_cpanel.py` çalıştırılır; betik yalnız güncel derleme varlıklarını yükler.

## Yedek ve kurtarma

- Sunucu yedeği AES-256-GCM ile şifrelenir.
- Her sunucu yedeğinin ardından `restore_check.py` geçici veritabanına gerçek geri yükleme yapar.
- Mac LaunchAgent günlük şifreli kopyayı ve ayrı anahtarı çeker, GCM etiketini doğrular ve 30 gün saklar.
- Mac, doğruladığı en güncel şifreli arşivi iki dönüşümlü cPanel yuvasından birine de yükler. Şifreleme anahtarı cPanel'e gönderilmez.
- Sunucu yedek anahtarı ile şifreli yedek aynı konumda tutulmamalıdır.

## Kişisel proje senkronizasyonu

- Mac'teki `Documents/ChatGPT` ve `Documents/Codex` klasörleri ev sunucusundaki `~/personal-sync` ile çift yönlü eşitlenir.
- `.stignore` kaynak dosyaları ve kullanıcı çıktılarını korurken `.git`, bağımlılık klasörleri, model önbellekleri, derleme çıktıları, geçici dosyalar ve logları dışlar.
- Elektrik veya ağ kesintisinden sonra Syncthing iki uçta otomatik başlar ve kalan değişiklikleri sürdürür.
- Canlıya alma, ilgili projenin kendi doğrulama ve yayın adımlarını tamamladıktan sonra yapılır; senkronizasyon tek başına yayın anlamına gelmez.

## Rota güvenliği

- Genel içerik: Antigravity Gemini, ardından Muse Contributor.
- Hassas/belirsiz içerik: Contributor içermeyen sabit Antigravity Gemini rotası.
- Şifreler, tokenlar, yetkilendirme başlıkları ve sistem sırları tüm model rotalarında engellenir.
- Uygulama sağlayıcı için sıfır saklama garantisi beyan etmez.

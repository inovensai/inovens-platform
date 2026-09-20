<?php
declare(strict_types=1);

$configPath = '/home/ino3d2saicom/inovens-hub-private/config.php';
if (!is_file($configPath)) {
    http_response_code(503);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(['error' => 'Sistem yapılandırması tamamlanıyor.'], JSON_UNESCAPED_UNICODE);
    exit;
}
$cfg = require $configPath;

ini_set('display_errors', '0');
ini_set('log_errors', '1');
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header("Content-Security-Policy: default-src 'none'; frame-ancestors 'none'");

session_name('inovens_hub');
session_set_cookie_params(['lifetime'=>86400*7,'path'=>'/','secure'=>true,'httponly'=>true,'samesite'=>'Lax']);
session_start();

function out(array $body, int $status = 200): never {
    http_response_code($status);
    echo json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}
function body(): array {
    $raw=file_get_contents('php://input');
    if($raw===''||$raw===false)return [];
    $v=json_decode($raw,true);if(!is_array($v))out(['error'=>'Geçersiz istek.'],400);return $v;
}
function clean_text(mixed $value,int $max,bool $required=false):string{
    $value=is_string($value)?trim($value):'';
    if(($required&&$value==='')||mb_strlen($value)>$max)out(['error'=>'Alanlardan biri geçersiz.'],422);return $value;
}
function db(array $cfg):PDO{
    static $pdo;if($pdo instanceof PDO)return $pdo;
    $pdo=new PDO("mysql:host={$cfg['DB_HOST']};dbname={$cfg['DB_NAME']};charset=utf8mb4",$cfg['DB_USER'],$cfg['DB_PASSWORD'],[PDO::ATTR_ERRMODE=>PDO::ERRMODE_EXCEPTION,PDO::ATTR_DEFAULT_FETCH_MODE=>PDO::FETCH_ASSOC,PDO::ATTR_EMULATE_PREPARES=>false]);return $pdo;
}
function migrate(PDO $db):void{
    $db->exec("CREATE TABLE IF NOT EXISTS users (id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,google_sub VARCHAR(128) NOT NULL UNIQUE,email VARCHAR(254) NOT NULL UNIQUE,name VARCHAR(180) NOT NULL,picture TEXT NULL,status ENUM('pending','active','revoked') NOT NULL DEFAULT 'pending',role ENUM('owner','president','vice_president','board') NOT NULL DEFAULT 'board',telegram_user_id VARCHAR(32) NULL UNIQUE,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,last_login_at TIMESTAMP NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");
    $db->exec("CREATE TABLE IF NOT EXISTS operations (id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,kind ENUM('task','event','decision','sponsor','project','risk') NOT NULL,title VARCHAR(220) NOT NULL,owner_name VARCHAR(160) NOT NULL DEFAULT '',due_date DATE NULL,status ENUM('candidate','open','in_progress','blocked','done','cancelled','confirmed','superseded') NOT NULL DEFAULT 'candidate',source_ref VARCHAR(300) NULL,note TEXT NULL,created_by BIGINT UNSIGNED NULL,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,INDEX(status),INDEX(kind),INDEX(due_date),CONSTRAINT fk_operations_user FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");
    $db->exec("CREATE TABLE IF NOT EXISTS content_drafts (id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,channel ENUM('instagram','linkedin','email','general') NOT NULL DEFAULT 'general',title VARCHAR(220) NOT NULL,body MEDIUMTEXT NOT NULL,asset_path TEXT NULL,status ENUM('draft','review','approved','published','rejected') NOT NULL DEFAULT 'draft',created_by BIGINT UNSIGNED NULL,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,CONSTRAINT fk_content_user FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");
    $db->exec("CREATE TABLE IF NOT EXISTS approvals (id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,action_type VARCHAR(80) NOT NULL,title VARCHAR(220) NOT NULL,preview TEXT NOT NULL,payload MEDIUMTEXT NOT NULL,payload_hash CHAR(64) NOT NULL,status ENUM('pending','approved','denied','executed','expired','failed') NOT NULL DEFAULT 'pending',requested_by BIGINT UNSIGNED NULL,decided_by BIGINT UNSIGNED NULL,expires_at TIMESTAMP NOT NULL,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,decided_at TIMESTAMP NULL,executed_at TIMESTAMP NULL,INDEX(status),INDEX(expires_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");
    $db->exec("CREATE TABLE IF NOT EXISTS bridge_jobs (id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,job_type VARCHAR(80) NOT NULL,payload MEDIUMTEXT NOT NULL,status ENUM('queued','processing','done','failed') NOT NULL DEFAULT 'queued',result MEDIUMTEXT NULL,requested_by BIGINT UNSIGNED NULL,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,claimed_at TIMESTAMP NULL,completed_at TIMESTAMP NULL,INDEX(status),INDEX(created_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");
    $db->exec("CREATE TABLE IF NOT EXISTS system_state (state_key VARCHAR(100) PRIMARY KEY,state_value MEDIUMTEXT NOT NULL,updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");
    $db->exec("CREATE TABLE IF NOT EXISTS audit_log (id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,user_id BIGINT UNSIGNED NULL,action VARCHAR(120) NOT NULL,target_type VARCHAR(80) NULL,target_id VARCHAR(100) NULL,detail TEXT NULL,ip_hash CHAR(64) NULL,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,INDEX(created_at),INDEX(action)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");
}
function audit(PDO $db,array $cfg,?int $userId,string $action,?string $targetType=null,?string $targetId=null,?array $detail=null):void{
    $ip=$_SERVER['REMOTE_ADDR']??'';$s=$db->prepare('INSERT INTO audit_log(user_id,action,target_type,target_id,detail,ip_hash) VALUES(?,?,?,?,?,?)');$s->execute([$userId,$action,$targetType,$targetId,$detail?json_encode($detail,JSON_UNESCAPED_UNICODE):null,hash_hmac('sha256',$ip,$cfg['APP_KEY'])]);
}
function user_view(array $u):array{return ['id'=>(int)$u['id'],'email'=>$u['email'],'name'=>$u['name'],'picture'=>$u['picture'],'status'=>$u['status'],'role'=>$u['role'],'telegram_linked'=>!empty($u['telegram_user_id'])];}
function session_user(PDO $db,bool $active=false):array{
    $id=$_SESSION['user_id']??null;if(!$id)out(['error'=>'Oturum gerekli.'],401);$s=$db->prepare('SELECT * FROM users WHERE id=?');$s->execute([$id]);$u=$s->fetch();if(!$u)out(['error'=>'Oturum bulunamadı.'],401);if($active&&$u['status']!=='active')out(['error'=>'Hesabınız henüz onaylanmadı.'],403);return $u;
}
function require_owner(array $u):void{if($u['role']!=='owner'||$u['status']!=='active')out(['error'=>'Bu işlem yalnız proje sahibine açıktır.'],403);}
function assert_write_origin(array $cfg):void{
    if($_SERVER['REQUEST_METHOD']==='GET')return;$origin=$_SERVER['HTTP_ORIGIN']??'';if($origin!==''&&rtrim($origin,'/')!==(parse_url($cfg['BASE_URL'],PHP_URL_SCHEME).'://'.parse_url($cfg['BASE_URL'],PHP_URL_HOST)))out(['error'=>'İstek kaynağı doğrulanamadı.'],403);$site=$_SERVER['HTTP_SEC_FETCH_SITE']??'';if($site!==''&&!in_array($site,['same-origin','none'],true))out(['error'=>'İstek kaynağı doğrulanamadı.'],403);
}
function bridge_auth(array $cfg):void{$h=$_SERVER['HTTP_AUTHORIZATION']??'';$token=str_starts_with($h,'Bearer ')?substr($h,7):'';if(!hash_equals($cfg['BRIDGE_TOKEN'],$token))out(['error'=>'Yetkisiz.'],401);}
function http_form(string $url,array $fields):array{
    $ch=curl_init($url);curl_setopt_array($ch,[CURLOPT_POST=>true,CURLOPT_POSTFIELDS=>http_build_query($fields),CURLOPT_RETURNTRANSFER=>true,CURLOPT_TIMEOUT=>25,CURLOPT_HTTPHEADER=>['Accept: application/json']]);$raw=curl_exec($ch);$status=(int)curl_getinfo($ch,CURLINFO_HTTP_CODE);$err=curl_error($ch);curl_close($ch);if($raw===false||$status<200||$status>=300)throw new RuntimeException('OAuth sağlayıcısına bağlanılamadı: '.$err);$data=json_decode($raw,true);if(!is_array($data))throw new RuntimeException('OAuth yanıtı geçersiz.');return $data;
}
function http_json(string $url,string $token):array{
    $ch=curl_init($url);curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_TIMEOUT=>20,CURLOPT_HTTPHEADER=>['Authorization: Bearer '.$token,'Accept: application/json']]);$raw=curl_exec($ch);$status=(int)curl_getinfo($ch,CURLINFO_HTTP_CODE);curl_close($ch);if($raw===false||$status!==200)throw new RuntimeException('Google profil bilgisi alınamadı.');$data=json_decode($raw,true);if(!is_array($data))throw new RuntimeException('Google profil yanıtı geçersiz.');return $data;
}

$db=db($cfg);migrate($db);assert_write_origin($cfg);$path=trim($_SERVER['PATH_INFO']??'','/');$method=$_SERVER['REQUEST_METHOD'];
try{
    require __DIR__.'/platform.php';
    if($path==='health'&&$method==='GET')out(['ok'=>true,'service'=>'inovens-hub']);
    if($path==='auth/google/start'&&$method==='GET'){
        if(empty($cfg['GOOGLE_CLIENT_ID'])||empty($cfg['GOOGLE_CLIENT_SECRET']))out(['error'=>'Google girişi yapılandırılıyor.'],503);$state=bin2hex(random_bytes(24));$verifier=rtrim(strtr(base64_encode(random_bytes(48)),'+/','-_'),'=');$_SESSION['oauth_state']=$state;$_SESSION['oauth_verifier']=$verifier;$bootstrap=$_GET['bootstrap']??'';$_SESSION['bootstrap_ok']=is_string($bootstrap)&&hash_equals($cfg['BOOTSTRAP_TOKEN'],$bootstrap);
        $params=['client_id'=>$cfg['GOOGLE_CLIENT_ID'],'redirect_uri'=>$cfg['BASE_URL'].'/api/index.php/auth/google/callback','response_type'=>'code','scope'=>'openid email profile','state'=>$state,'prompt'=>'select_account','code_challenge'=>rtrim(strtr(base64_encode(hash('sha256',$verifier,true)),'+/','-_'),'='),'code_challenge_method'=>'S256'];header('Location: https://accounts.google.com/o/oauth2/v2/auth?'.http_build_query($params));exit;
    }
    if($path==='auth/google/callback'&&$method==='GET'){
        $state=$_GET['state']??'';$code=$_GET['code']??'';if(!is_string($state)||!hash_equals($_SESSION['oauth_state']??'',$state)||!is_string($code)||$code==='')out(['error'=>'Google giriş doğrulaması başarısız.'],400);
        $tokens=http_form('https://oauth2.googleapis.com/token',['client_id'=>$cfg['GOOGLE_CLIENT_ID'],'client_secret'=>$cfg['GOOGLE_CLIENT_SECRET'],'code'=>$code,'grant_type'=>'authorization_code','redirect_uri'=>$cfg['BASE_URL'].'/api/index.php/auth/google/callback','code_verifier'=>$_SESSION['oauth_verifier']??'']);$profile=http_json('https://openidconnect.googleapis.com/v1/userinfo',(string)($tokens['access_token']??''));if(empty($profile['sub'])||empty($profile['email'])||empty($profile['email_verified']))out(['error'=>'Doğrulanmış Google e-postası gerekli.'],403);session_regenerate_id(true);
        $ownerCount=(int)$db->query("SELECT COUNT(*) FROM users WHERE role='owner'")->fetchColumn();$isOwner=$ownerCount===0&&!empty($_SESSION['bootstrap_ok']);$s=$db->prepare("INSERT INTO users(google_sub,email,name,picture,status,role,last_login_at) VALUES(?,?,?,?,?,?,NOW()) ON DUPLICATE KEY UPDATE google_sub=VALUES(google_sub),name=VALUES(name),picture=VALUES(picture),last_login_at=NOW()");$s->execute([(string)$profile['sub'],mb_strtolower((string)$profile['email']),(string)($profile['name']??$profile['email']),$profile['picture']??null,$isOwner?'active':'pending',$isOwner?'owner':'member']);$q=$db->prepare('SELECT * FROM users WHERE email=?');$q->execute([mb_strtolower((string)$profile['email'])]);$u=$q->fetch();$_SESSION['user_id']=(int)$u['id'];unset($_SESSION['oauth_state'],$_SESSION['oauth_verifier'],$_SESSION['bootstrap_ok']);audit($db,$cfg,(int)$u['id'],'auth.login','user',(string)$u['id']);header('Location: '.$cfg['BASE_URL'].'/');exit;
    }
    if($path==='auth/me'&&$method==='GET'){$id=$_SESSION['user_id']??null;if(!$id)out(['user'=>null]);$s=$db->prepare('SELECT * FROM users WHERE id=?');$s->execute([$id]);$u=$s->fetch();$_SESSION['csrf_token']??=bin2hex(random_bytes(24));out(['user'=>$u?user_view($u):null,'csrf_token'=>$_SESSION['csrf_token']]);}
    if($path==='auth/logout'&&$method==='POST'){session_destroy();out(['ok'=>true]);}
    if(!str_starts_with($path,'auth/')&&$path!=='health')out(['error'=>'Bu uç nokta yeni platforma taşındı. Paneli yenileyin.'],410);
    if($path==='summary'&&$method==='GET'){
        session_user($db,true);$counts=[];foreach($db->query('SELECT status,COUNT(*) c FROM operations GROUP BY status')as$r)$counts[$r['status']]=(int)$r['c'];$counts['events']=(int)$db->query("SELECT COUNT(*) FROM operations WHERE kind='event' AND status NOT IN ('done','cancelled','superseded')")->fetchColumn();$pendingUsers=(int)$db->query("SELECT COUNT(*) FROM users WHERE status='pending'")->fetchColumn();$pendingApprovals=(int)$db->query("SELECT COUNT(*) FROM approvals WHERE status='pending' AND expires_at>NOW()")->fetchColumn();$last=$db->query("SELECT state_value,updated_at FROM system_state WHERE state_key='bridge_heartbeat'")->fetch();$online=$last&&strtotime($last['updated_at'])>time()-90;out(['counts'=>$counts,'pending_users'=>$pendingUsers,'pending_approvals'=>$pendingApprovals,'bridge_online'=>$online,'last_bridge_seen'=>$last['updated_at']??null]);
    }
    if($path==='operations'&&$method==='GET'){session_user($db,true);$items=$db->query('SELECT * FROM operations ORDER BY FIELD(status,"open","in_progress","blocked","candidate","confirmed","done","cancelled","superseded"),due_date IS NULL,due_date,id DESC LIMIT 250')->fetchAll();out(['items'=>$items]);}
    if($path==='operations'&&$method==='POST'){
        $u=session_user($db,true);$b=body();$kinds=['task','event','decision','sponsor','project','risk'];$statuses=['candidate','open','in_progress','blocked','done','cancelled','confirmed','superseded'];$kind=in_array($b['kind']??'',$kinds,true)?$b['kind']:'task';$status=in_array($b['status']??'',$statuses,true)?$b['status']:'candidate';$title=clean_text($b['title']??'',220,true);$owner=clean_text($b['owner_name']??'',160);$source=clean_text($b['source_ref']??'',300);$note=clean_text($b['note']??'',5000);$due=clean_text($b['due_date']??'',10);if($due!==''&&!preg_match('/^\d{4}-\d{2}-\d{2}$/',$due))out(['error'=>'Tarih geçersiz.'],422);$s=$db->prepare('INSERT INTO operations(kind,title,owner_name,due_date,status,source_ref,note,created_by) VALUES(?,?,?,?,?,?,?,?)');$s->execute([$kind,$title,$owner,$due?:null,$status,$source?:null,$note?:null,$u['id']]);$id=(int)$db->lastInsertId();audit($db,$cfg,(int)$u['id'],'operation.create','operation',(string)$id,['kind'=>$kind,'title'=>$title]);out(['id'=>$id],201);
    }
    if($path==='users'&&$method==='GET'){$u=session_user($db,true);require_owner($u);$rows=$db->query("SELECT * FROM users ORDER BY FIELD(role,'owner','president','vice_president','board'),FIELD(status,'pending','active','revoked'),created_at")->fetchAll();out(['users'=>array_map('user_view',$rows)]);}
    if(preg_match('#^users/(\d+)$#',$path,$m)&&$method==='PATCH'){
        $u=session_user($db,true);require_owner($u);$id=(int)$m[1];$b=body();$status=$b['status']??null;$role=$b['role']??null;if($id===(int)$u['id'])out(['error'=>'Proje sahibi kendi erişimini değiştiremez.'],422);if(!in_array($status,['pending','active','revoked'],true)||!in_array($role,['president','vice_president','board'],true))out(['error'=>'Rol veya durum geçersiz.'],422);$s=$db->prepare("UPDATE users SET status=?,role=?,telegram_user_id=IF(?='revoked',NULL,telegram_user_id) WHERE id=? AND role<>'owner'");$s->execute([$status,$role,$status,$id]);audit($db,$cfg,(int)$u['id'],'user.update','user',(string)$id,['status'=>$status,'role'=>$role]);out(['ok'=>true]);
    }
    if($path==='approvals'&&$method==='GET'){session_user($db,true);$db->exec("UPDATE approvals SET status='expired' WHERE status='pending' AND expires_at<=NOW()");$rows=$db->query("SELECT a.id,a.action_type,a.title,a.preview,a.status,a.created_at,COALESCE(u.name,'Sistem') requested_by_name FROM approvals a LEFT JOIN users u ON u.id=a.requested_by WHERE a.status='pending' ORDER BY a.id DESC")->fetchAll();out(['items'=>$rows]);}
    if(preg_match('#^approvals/(\d+)$#',$path,$m)&&$method==='POST'){
        $u=session_user($db,true);$b=body();$decision=$b['decision']??'';if(!in_array($decision,['approved','denied'],true))out(['error'=>'Karar geçersiz.'],422);$db->beginTransaction();$s=$db->prepare("SELECT * FROM approvals WHERE id=? AND status='pending' AND expires_at>NOW() FOR UPDATE");$s->execute([(int)$m[1]]);$a=$s->fetch();if(!$a){$db->rollBack();out(['error'=>'İşlem artık onaylanabilir durumda değil.'],409);}$s=$db->prepare('UPDATE approvals SET status=?,decided_by=?,decided_at=NOW() WHERE id=?');$s->execute([$decision,$u['id'],$a['id']]);if($decision==='approved'){$s=$db->prepare("INSERT INTO bridge_jobs(job_type,payload,requested_by) VALUES('execute_approval',?,?)");$s->execute([json_encode(['approval_id'=>(int)$a['id'],'payload_hash'=>$a['payload_hash']],JSON_UNESCAPED_UNICODE),$u['id']]);}$db->commit();audit($db,$cfg,(int)$u['id'],'approval.'.$decision,'approval',(string)$a['id']);out(['ok'=>true]);
    }
    if($path==='telegram/link'&&$method==='POST'){$u=session_user($db,true);$code=strtoupper(clean_text(body()['code']??'',8,true));if(!preg_match('/^[A-HJ-NP-Z2-9]{8}$/',$code))out(['error'=>'Eşleştirme kodu geçersiz.'],422);$s=$db->prepare("INSERT INTO bridge_jobs(job_type,payload,requested_by) VALUES('telegram_pair',?,?)");$s->execute([json_encode(['code'=>$code,'user_id'=>(int)$u['id']],JSON_UNESCAPED_UNICODE),$u['id']]);audit($db,$cfg,(int)$u['id'],'telegram.link.request','user',(string)$u['id']);out(['message'=>'Kod doğrulanmak üzere güvenli kuyruğa alındı.']);}
    if($path==='bridge/operations/import'&&$method==='POST'){
        bridge_auth($cfg);$items=body()['items']??null;if(!is_array($items)||count($items)>500)out(['error'=>'İçe aktarma paketi geçersiz.'],422);$kinds=['task','event','decision','sponsor','project','risk'];$statuses=['candidate','open','in_progress','blocked','done','cancelled','confirmed','superseded'];$find=$db->prepare('SELECT id FROM operations WHERE source_ref=? LIMIT 1');$insert=$db->prepare('INSERT INTO operations(kind,title,owner_name,due_date,status,source_ref,note) VALUES(?,?,?,?,?,?,?)');$update=$db->prepare('UPDATE operations SET kind=?,title=?,owner_name=?,due_date=?,status=?,note=? WHERE id=?');$created=0;$updated=0;$db->beginTransaction();foreach($items as$item){if(!is_array($item))continue;$kind=in_array($item['kind']??'',$kinds,true)?$item['kind']:'task';$status=in_array($item['status']??'',$statuses,true)?$item['status']:'candidate';$title=clean_text($item['title']??'',220,true);$owner=clean_text($item['owner']??$item['owner_name']??'',160);$source=clean_text($item['source']??$item['source_ref']??'',300,true);$note=clean_text($item['note']??'',5000);$due=clean_text($item['due']??$item['due_date']??'',10);if($due!==''&&!preg_match('/^\d{4}-\d{2}-\d{2}$/',$due))$due='';$find->execute([$source]);$id=$find->fetchColumn();if($id){$update->execute([$kind,$title,$owner,$due?:null,$status,$note?:null,(int)$id]);$updated++;}else{$insert->execute([$kind,$title,$owner,$due?:null,$status,$source,$note?:null]);$created++;}}$db->commit();out(['ok'=>true,'created'=>$created,'updated'=>$updated]);
    }
    if($path==='bridge/users/telegram'&&$method==='POST'){
        bridge_auth($cfg);$b=body();$email=mb_strtolower(clean_text($b['email']??'',254,true));$telegram=clean_text($b['telegram_user_id']??'',32,true);if(!filter_var($email,FILTER_VALIDATE_EMAIL)||!ctype_digit($telegram))out(['error'=>'Hesap eşleştirme verisi geçersiz.'],422);$s=$db->prepare("UPDATE users SET telegram_user_id=? WHERE email=? AND status='active'");$s->execute([$telegram,$email]);if($s->rowCount()!==1)out(['error'=>'Etkin kullanıcı bulunamadı.'],404);out(['ok'=>true]);
    }
    if($path==='bridge/jobs'&&$method==='GET'){
        bridge_auth($cfg);$s=$db->prepare("INSERT INTO system_state(state_key,state_value) VALUES('bridge_heartbeat',?) ON DUPLICATE KEY UPDATE state_value=VALUES(state_value)");$s->execute([json_encode(['host'=>'ege-kasa'])]);$db->exec("UPDATE bridge_jobs SET status='queued',claimed_at=NULL WHERE status='processing' AND claimed_at<NOW()-INTERVAL 5 MINUTE");$db->beginTransaction();$rows=$db->query("SELECT id,job_type,payload FROM bridge_jobs WHERE status='queued' ORDER BY id LIMIT 10 FOR UPDATE")->fetchAll();if($rows){$ids=implode(',',array_map(fn($r)=>(int)$r['id'],$rows));$db->exec("UPDATE bridge_jobs SET status='processing',claimed_at=NOW() WHERE id IN ($ids)");}$db->commit();out(['jobs'=>array_map(fn($r)=>['id'=>(int)$r['id'],'job_type'=>$r['job_type'],'payload'=>json_decode($r['payload'],true)],$rows)]);
    }
    if(preg_match('#^bridge/jobs/(\d+)$#',$path,$m)&&$method==='POST'){
        bridge_auth($cfg);$b=body();$ok=!empty($b['ok']);$result=json_encode($b['result']??[],JSON_UNESCAPED_UNICODE);$s=$db->prepare("UPDATE bridge_jobs SET status=?,result=?,completed_at=NOW() WHERE id=? AND status='processing'");$s->execute([$ok?'done':'failed',$result,(int)$m[1]]);if(isset($b['telegram_user_id'],$b['user_id'])&&$ok){$s=$db->prepare("UPDATE users SET telegram_user_id=? WHERE id=? AND status='active'");$s->execute([(string)$b['telegram_user_id'],(int)$b['user_id']]);}out(['ok'=>true]);
    }
    out(['error'=>'Uç nokta bulunamadı.'],404);
}catch(Throwable $e){error_log('INOVENS Hub: '.$e->getMessage());out(['error'=>'Sunucu işlemi tamamlayamadı.'],500);}

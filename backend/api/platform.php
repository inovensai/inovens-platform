<?php
/* Versioned transport only. Runtime is authoritative for roles, quotas and content. */
function platform_schema(PDO $db):void {
    $db->exec("CREATE TABLE IF NOT EXISTS platform_requests(id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,user_id BIGINT UNSIGNED NOT NULL,payload LONGTEXT NOT NULL,status VARCHAR(20) NOT NULL DEFAULT 'queued',response LONGTEXT NULL,claim_token CHAR(48) NULL,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,claimed_at TIMESTAMP NULL,completed_at TIMESTAMP NULL,INDEX(status,created_at),INDEX(user_id,id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4");
}
function transport_encrypt(array $value,array $cfg):string {
    $iv=random_bytes(12);$tag='';$raw=openssl_encrypt(json_encode($value,JSON_UNESCAPED_UNICODE|JSON_UNESCAPED_SLASHES),'aes-256-gcm',hash('sha256',$cfg['APP_KEY'],true),OPENSSL_RAW_DATA,$iv,$tag);
    if($raw===false)throw new RuntimeException('Transport encryption failed');return base64_encode($iv.$raw.$tag);
}
function transport_decrypt(string $value,array $cfg):array {
    $raw=base64_decode($value,true);if($raw===false||strlen($raw)<29)throw new RuntimeException('Invalid transport');
    $plain=openssl_decrypt(substr($raw,12,-16),'aes-256-gcm',hash('sha256',$cfg['APP_KEY'],true),OPENSSL_RAW_DATA,substr($raw,0,12),substr($raw,-16));
    $v=$plain!==false?json_decode($plain,true):null;if(!is_array($v))throw new RuntimeException('Invalid transport');return $v;
}
if(str_starts_with($path,'v2/')||str_starts_with($path,'bridge/platform/')) {
    platform_schema($db);
    if($path==='v2/rpc'&&$method==='POST') {
        $u=session_user($db,false);
        if(!hash_equals($_SESSION['csrf_token']??'',$_SERVER['HTTP_X_CSRF_TOKEN']??'')||empty($_SESSION['csrf_token']))out(['error'=>'Oturum doğrulanamadı. Sayfayı yenileyin.'],403);
        $b=body();$p=$b['path']??'';$verb=$b['method']??'GET';
        if(!is_string($p)||!preg_match('#^[a-z0-9/_-]{1,150}$#',$p)||!in_array($verb,['GET','POST','PATCH','DELETE'],true))out(['error'=>'İstek geçersiz.'],422);
        $n=$db->prepare("SELECT count(*) FROM platform_requests WHERE user_id=? AND status IN ('queued','processing') AND created_at>DATE_SUB(NOW(),INTERVAL 2 MINUTE)");$n->execute([$u['id']]);if((int)$n->fetchColumn()>=15)out(['error'=>'Bekleyen istekler tamamlanıyor. Biraz sonra tekrar deneyin.'],429);
        $data=$b['body']??[];if(!is_array($data))out(['error'=>'İstek gövdesi geçersiz.'],422);
        $payload=transport_encrypt(['uid'=>(int)$u['id'],'path'=>$p,'method'=>$verb,'body'=>$data],$cfg);
        $s=$db->prepare('INSERT INTO platform_requests(user_id,payload) VALUES(?,?)');$s->execute([$u['id'],$payload]);out(['request_id'=>(int)$db->lastInsertId()],202);
    }
    if(preg_match('#^v2/result/(\d+)$#',$path,$m)&&$method==='GET') {
        $u=session_user($db,false);$s=$db->prepare('SELECT status,response,created_at FROM platform_requests WHERE id=? AND user_id=?');$s->execute([(int)$m[1],$u['id']]);$r=$s->fetch();
        if(!$r)out(['error'=>'İstek bulunamadı.'],404);
        if($r['status']==='done'&&$r['response'])out(transport_decrypt($r['response'],$cfg));
        if($r['status']==='expired'||strtotime($r['created_at'])<time()-120)out(['ok'=>false,'error'=>'Sunucu bağlantısı bekleniyor. Biraz sonra tekrar deneyin.','code'=>'bridge_offline','status'=>503]);
        out(['pending'=>true],202);
    }
    if($path==='bridge/platform/sync'&&$method==='GET') {
        bridge_auth($cfg);
        $roleType=$db->query("SHOW COLUMNS FROM users LIKE 'role'")->fetch()['Type']??'';
        if(!str_contains($roleType,"'member'"))$db->exec("ALTER TABLE users MODIFY role ENUM('owner','president','vice_president','board','member') NOT NULL DEFAULT 'member'");
        $users=$db->query('SELECT id,email,name,role,status,telegram_user_id FROM users')->fetchAll();
        foreach($users as&$x)$x['id']=(int)$x['id'];
        $ops=$db->query('SELECT kind,title,owner_name,due_date,status,source_ref,note FROM operations ORDER BY id LIMIT 500')->fetchAll();
        out(['users'=>$users,'operations'=>$ops]);
    }
    if($path==='bridge/platform/jobs'&&$method==='GET') {
        bridge_auth($cfg);session_write_close();
        $db->exec("UPDATE platform_requests SET status='expired',payload='' WHERE status IN ('queued','processing') AND created_at<DATE_SUB(NOW(),INTERVAL 2 MINUTE)");
        $db->exec("DELETE FROM platform_requests WHERE created_at<DATE_SUB(NOW(),INTERVAL 24 HOUR)");
        $db->beginTransaction();$rows=$db->query("SELECT id,payload,user_id FROM platform_requests WHERE status='queued' ORDER BY id LIMIT 20 FOR UPDATE")->fetchAll();
        $jobs=[];$s=$db->prepare("UPDATE platform_requests SET status='processing',claim_token=?,claimed_at=NOW() WHERE id=?");
        foreach($rows as$r){$claim=bin2hex(random_bytes(24));$s->execute([$claim,$r['id']]);$jobs[]=['id'=>(int)$r['id'],'user_id'=>(int)$r['user_id'],'claim'=>$claim,'payload'=>$r['payload']];}
        $db->commit();out(['jobs'=>$jobs]);
    }
    if($path==='bridge/platform/results'&&$method==='POST') {
        bridge_auth($cfg);$b=body();$s=$db->prepare("UPDATE platform_requests SET response=?,status='done',completed_at=NOW(),payload='' WHERE id=? AND claim_token=? AND status='processing'");
        foreach(($b['items']??[]) as$r){if(!is_array($r))continue;$s->execute([$r['response'],(int)$r['id'],$r['claim']]);}
        out(['ok'=>true]);
    }
    if($path==='bridge/platform/heartbeat'&&$method==='POST') {
        bridge_auth($cfg);$b=body();$s=$db->prepare("INSERT INTO system_state(state_key,state_value) VALUES('platform_heartbeat',?) ON DUPLICATE KEY UPDATE state_value=VALUES(state_value),updated_at=NOW()");$s->execute([json_encode(['version'=>'2.0.0'])]);
        $update=$db->prepare('UPDATE users SET status=?,role=?,telegram_user_id=? WHERE id=?');
        foreach(($b['users']??[]) as$u){if(!in_array($u['role'],['owner','president','vice_president','board','member'],true)||!in_array($u['status'],['active','pending','revoked'],true))continue;$update->execute([$u['status'],$u['role'],$u['telegram_id'],(int)$u['id']]);}
        out(['ok'=>true]);
    }
    out(['error'=>'İşlem bulunamadı.'],404);
}

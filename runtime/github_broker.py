"""Owner-only GitHub operations for INOVENS, isolated from AI-Ege's gh profile."""
import os
import re
import subprocess
from common import Denied

CONFIG_DIR='/home/ege/.config/gh-inovens'
ACCOUNT='inovensai'
REPO_NAME=re.compile(r'^[A-Za-z0-9_.-]{1,100}$')

def run_gh(args):
    env={**os.environ,'GH_CONFIG_DIR':CONFIG_DIR,'GH_PAGER':'cat','PAGER':'cat'}
    try:
        result=subprocess.run(['/usr/bin/gh',*args],env=env,capture_output=True,text=True,timeout=45,check=False)
    except (OSError,subprocess.TimeoutExpired):
        raise Denied('github_unavailable','GitHub bağlantısına şu anda erişilemiyor.',503)
    if result.returncode:
        # gh stderr may contain private data; never return it to the model.
        raise Denied('github_operation_failed','GitHub işlemi tamamlanamadı; bağlantıyı veya depo yetkisini kontrol edin.',502)
    return result.stdout.strip()[:20000]

def connected_account():
    try:return run_gh(['api','user','--jq','.login'])
    except Denied:return None

def require_account():
    if connected_account()!=ACCOUNT:
        raise Denied('github_not_connected','INOVENS GitHub hesabı henüz bağlanmadı veya beklenen hesapla eşleşmiyor.',503)

def repo_name(value):
    value=str(value or '').strip()
    if not REPO_NAME.fullmatch(value) or value in {'.','..'}:
        raise Denied('github_repo_invalid','Geçerli bir inovensai depo adı gerekli.',422)
    return ACCOUNT+'/'+value

def number(value):
    if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=1_000_000_000:
        raise Denied('github_number_invalid','Geçerli bir kayıt numarası gerekli.',422)
    return str(value)

def limit(value):
    if value is None:return '30'
    if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=100:
        raise Denied('github_limit_invalid','Sınır 1 ile 100 arasında olmalı.',422)
    return str(value)

def text(value,field,max_length=50000):
    if not isinstance(value,str) or not value.strip() or len(value)>max_length:
        raise Denied('github_input_invalid',field+' gerekli veya fazla uzun.',422)
    return value.strip()

def call(name,args):
    if not isinstance(args,dict):raise Denied('github_input_invalid','GitHub araç girdisi geçersiz.',422)
    action=args.get('action')
    if name=='github_read' and action=='status':
        account=connected_account()
        return {'connected':account==ACCOUNT,'account':account if account==ACCOUNT else None,'profile':'inovens'}
    require_account()
    if name=='github_read':
        if action=='list_repos':
            return {'result':run_gh(['repo','list',ACCOUNT,'--limit',limit(args.get('limit')),'--json','name,description,isPrivate,updatedAt,url,defaultBranchRef'])}
        repo=repo_name(args.get('repo'))
        if action=='repo':return {'result':run_gh(['repo','view',repo,'--json','name,description,isPrivate,defaultBranchRef,url,updatedAt'])}
        if action in {'issues','pulls'}:
            state=args.get('state','open')
            if state not in {'open','closed','all'}:raise Denied('github_state_invalid','Geçersiz durum filtresi.',422)
            subcommand='issue' if action=='issues' else 'pr'
            fields='number,title,state,author,updatedAt,url,labels' if action=='issues' else 'number,title,state,author,headRefName,baseRefName,updatedAt,url,statusCheckRollup'
            return {'result':run_gh([subcommand,'list','--repo',repo,'--limit',limit(args.get('limit')),'--state',state,'--json',fields])}
        if action in {'issue','pull'}:
            subcommand='issue' if action=='issue' else 'pr'
            fields='number,title,body,state,author,comments,labels,url' if action=='issue' else 'number,title,body,state,author,headRefName,baseRefName,mergeable,reviews,statusCheckRollup,url'
            return {'result':run_gh([subcommand,'view',number(args.get('number')),'--repo',repo,'--json',fields])}
        if action=='runs':return {'result':run_gh(['run','list','--repo',repo,'--limit',limit(args.get('limit')),'--json','databaseId,name,status,conclusion,headBranch,event,createdAt,updatedAt,url'])}
    elif name=='github_write':
        if action=='create_repo':
            repo=repo_name(args.get('repo'))
            visibility=args.get('visibility','private')
            if visibility not in {'private','public'}:
                raise Denied('github_visibility_invalid','Depo görünürlüğü private veya public olmalı.',422)
            command=['repo','create',repo,'--'+visibility,'--add-readme']
            description=args.get('description','')
            if description:
                command.extend(['--description',text(description,'Açıklama',500)])
            return {'result':run_gh(command)}
        repo=repo_name(args.get('repo'))
        if action=='create_issue':
            return {'result':run_gh(['issue','create','--repo',repo,'--title',text(args.get('title'),'Başlık',300),'--body',text(args.get('body'),'İçerik')])}
        if action=='comment_issue':
            return {'result':run_gh(['issue','comment',number(args.get('number')),'--repo',repo,'--body',text(args.get('body'),'Yorum')])}
        if action=='create_pull':
            head=text(args.get('head'),'Kaynak dal',200);base=text(args.get('base','main'),'Hedef dal',200)
            if not re.fullmatch(r'[A-Za-z0-9_./-]+',head) or not re.fullmatch(r'[A-Za-z0-9_./-]+',base) or '..' in head or '..' in base:
                raise Denied('github_branch_invalid','Geçersiz dal adı.',422)
            command=['pr','create','--repo',repo,'--title',text(args.get('title'),'Başlık',300),'--body',text(args.get('body'),'İçerik'),'--head',head,'--base',base]
            if args.get('draft') is True:command.append('--draft')
            return {'result':run_gh(command)}
        if action=='comment_pull':
            return {'result':run_gh(['pr','comment',number(args.get('number')),'--repo',repo,'--body',text(args.get('body'),'Yorum')])}
    raise Denied('github_action_invalid','Desteklenmeyen GitHub işlemi.',422)

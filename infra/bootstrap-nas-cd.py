#!/usr/bin/env python3
"""One-time Mac setup over existing SSH; credential input is hidden and never logged."""
import argparse
import base64
import getpass
import json
import os
import shlex
import subprocess
from pathlib import Path

REMOTE = r'''
import base64,fcntl,json,os,pathlib,subprocess,sys,time
payload=json.load(sys.stdin)
base=pathlib.Path('/path/to/nas/home/docker/monica-next')
root=base/'infra';cd=base/'cd'
if payload.get('token'):
 cd.mkdir(mode=0o700,exist_ok=True);os.chmod(str(cd),0o700)
 target=cd/'github-token'
 temporary=cd/'github-token.next'
 with os.fdopen(os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as stream:
  os.chmod(str(temporary),0o600);stream.write(payload['token']+'\n');stream.flush();os.fsync(stream.fileno())
 temporary.replace(target)
 print(json.dumps({'credential_installed':True,'path':str(target)}))
 raise SystemExit(0)
with (root/'.deploy.lock').open('a') as lock:
 try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError: raise SystemExit('Existing deployment owns the canonical lock; setup made no changes')
 if (root/'.deploy-journal.json').exists() or (root/'.cd-install.json').exists():
  raise SystemExit('Existing deployment recovery must finish; setup made no changes')
 cd.mkdir(mode=0o700,exist_ok=True);os.chmod(str(cd),0o700)
 version=cd/'versions'/payload['version'];version.mkdir(parents=True,exist_ok=True)
 for name,encoded in payload['files'].items():
  target=version/name;target.write_bytes(base64.b64decode(encoded));os.chmod(str(target),0o700)
 config={'infra':str(root),'state_dir':str(cd),'token_file':str(cd/'github-token'),'ghcr_expires':'2027-01-14','github_expires':'2027-01-16'}
 temporary=cd/'config.next';temporary.write_text(json.dumps(config,indent=2)+'\n');os.chmod(str(temporary),0o600);temporary.replace(cd/'config.json')
 link=cd/'current.next'
 if link.is_symlink():link.unlink()
 link.symlink_to(version);link.replace(cd/'current')
 (cd/'paused').touch(mode=0o600)
 def api(method,**params):
  endpoint='SYNO.Core.TaskScheduler.Root' if method in ('create','set') else 'SYNO.Core.TaskScheduler'
  version=2 if method=='list' else 4
  q=subprocess.run(['/usr/syno/bin/synowebapi','--exec-fastwebapi','api='+endpoint,'method='+method,'version='+str(version),*[k+'='+json.dumps(v) for k,v in params.items()]],capture_output=True,text=True,timeout=120,check=True)
  result=json.loads(q.stdout[q.stdout.index('{'):])
  if not result.get('success'):raise RuntimeError('DSM scheduler API rejected '+method+': '+str(result.get('error',{}).get('code')))
  return result['data']
 existing=api('list')['tasks']
 schedule={'date':time.strftime('%Y/%m/%d'),'repeat_date':1001,'date_type':0,'week_day':'0,1,2,3,4,5,6','hour':0,'minute':0,
           'repeat_hour':0,'repeat_min':2,'last_work_hour':23,'monthly_week':[]}
 command='/usr/bin/python3 '+str(cd/'current/nas-cd.py')+' --config '+str(cd/'config.json')
 ids=[]
 for action in ('deploy','cleanup'):
  name='Monica CD '+action
  matches=[t for t in existing if t['name']==name]
  if len(matches)>1:raise RuntimeError('Duplicate DSM task names need review')
  extra={'notify_enable':False,'notify_if_error':False,'notify_mail':'',
         'script':'umask 077; '+command+' '+('run-once' if action=='deploy' else 'cleanup')+' --quiet >> '+str(cd/'scheduler.log')+' 2>&1'}
  params={'name':name,'owner':'root','real_owner':'root','enable':True,'schedule':schedule,'extra':extra,'type':'script'}
  if matches:
   task=matches[0]
   if task['owner']!='root' or task['type']!='script':raise RuntimeError('Unexpected existing task owner/type')
   before=api('get',id=task['id'])
   backup=cd/('scheduler-backup-'+str(task['id'])+'-'+str(time.time_ns())+'.json')
   backup.write_text(json.dumps(before));os.chmod(str(backup),0o600)
   api('set',id=task['id'],**params);task_id=task['id']
  else:task_id=api('create',**params)['id']
  verified=api('get',id=task_id)
  if verified['owner']!='root' or verified['schedule']['repeat_min']!=2 or verified['extra']['script']!=extra['script']:
   raise RuntimeError('DSM task verification failed')
  ids.append(task_id)
 (cd/'scheduler-tasks.json').write_text(json.dumps({'ids':ids,'names':['Monica CD deploy','Monica CD cleanup']}))
 os.chmod(str(cd/'scheduler-tasks.json'),0o600)
 print(json.dumps({'installed':str(version),'paused':True,'task_ids':ids,'tasks':2}))

'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provision-token', action='store_true')
    args = parser.parse_args()
    password = os.environ.get('NAS_PASSWORD') or getpass.getpass('NAS sudo password: ')
    if args.provision_token:
        token = getpass.getpass('Fine-grained GitHub token (hidden): ').strip()
        if not token.startswith('github_pat_'):
            raise SystemExit('Use a repository-scoped fine-grained token')
        payload = {'token': token}
    else:
        directory = Path(__file__).resolve().parent
        version = subprocess.check_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'], text=True).strip()
        if subprocess.check_output(['git', '-C', str(directory), 'status', '--porcelain'], text=True).strip():
            raise SystemExit('Commit and review the checkout before installing the executor')
        payload = {'version': version, 'files': {name: base64.b64encode((directory / name).read_bytes()).decode()
                    for name in ('nas-cd.py', 'release_common.py', 'release_installer.py')}}
    command = ['ssh', '-o', 'BatchMode=yes', 'nas',
               shlex.join(['sudo', '-k', '-S', '-p', '', 'python3', '-c', REMOTE])]
    result = subprocess.run(command, input=password + '\n' + json.dumps(payload), text=True, capture_output=True, check=False)
    print(result.stdout, end='')
    if result.returncode:
        raise SystemExit(result.stderr.strip() or 'NAS setup failed')


if __name__ == '__main__':
    main()

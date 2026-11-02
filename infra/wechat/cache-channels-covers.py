"""Cache Channels covers locally with the existing prefetch lock and request pacing."""
import fcntl
import hashlib
import importlib.util
import json
import random
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path('/volume1/docker/monica-next/data/staging/wechat-media-backfill-20260920')
for name in ('moments_xml',):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
cache=ROOT/'channel-covers';cache.mkdir(exist_ok=True)
refs=[];urls={}
with sqlite3.connect('file:/volume1/docker/monica-next/data/staging/media-snapshot-20260919/sns.db?mode=ro',uri=True) as db:
    for tid,xml in db.execute("SELECT tid,content FROM SnsTimeLine WHERE content LIKE '%<finderFeed>%' "):
        try:r,_=m.parse_moments_xml(xml);obj=r.find('TimelineObject')
        except ET.ParseError:continue
        if obj is None:continue
        for pos,media in enumerate(m.finder_media(obj),1):
            url=media.get('thumbnail_url')
            if not url:continue
            key=hashlib.sha256(url.encode()).hexdigest()+'.bin'
            refs.append({'post':obj.findtext('id') or str(tid),'role':f'thumbnail-{pos}','url':url,'file':key})
            urls[key]=url
(ROOT/'channels-cover-manifest.json').write_text(json.dumps({'references':refs}))
report={'total_references':len(refs),'unique_urls':len(urls),'cached':0,'failed':0,'results':[],'state':'running'}
def save():
    p=ROOT/'channels-downloads.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(report));tmp.replace(p)
class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if urlsplit(newurl).hostname not in {'wxapp.tc.qq.com','finder.video.qq.com'}:raise ValueError('Unapproved redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),Redirect())
with open('/path/to/nas/home/docker/chatlog-alpha/.sns-prefetch.lock','a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    for key,url in urls.items():
        path=cache/key;result={'file':key}
        try:
            if not path.exists():
                parsed=urlsplit(url)
                if parsed.scheme not in ('http','https') or parsed.hostname not in {'wxapp.tc.qq.com','finder.video.qq.com'}:raise ValueError('Unapproved cover URL')
                time.sleep(random.uniform(.5,2))
                with opener.open(url,timeout=25) as res:
                    data=res.read(25*1024*1024+1)
                if len(data)>25*1024*1024 or not (data.startswith((b'\xff\xd8\xff',b'\x89PNG',b'GIF8')) or data[8:12]==b'WEBP'):raise ValueError('Invalid cover image')
                tmp=path.with_suffix('.tmp');tmp.write_bytes(data);tmp.replace(path)
            report['cached']+=1;result.update(status='cached',bytes=path.stat().st_size)
        except (OSError,ValueError,HTTPException) as ex:
            report['failed']+=1;result.update(status='failed',error=type(ex).__name__,http=getattr(ex,'code',None))
        report['results'].append(result);save()
        if len(report['results'])%25==0:print(json.dumps({k:v for k,v in report.items() if k!='results'}),flush=True)
report['state']='complete';save();print(json.dumps({k:v for k,v in report.items() if k!='results'}),flush=True)

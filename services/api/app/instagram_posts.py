"""Source-backed Instagram post metadata; URLs are references, never UI fallbacks."""
import hashlib
import json
import re
from datetime import UTC, datetime


def user_identity(value):
    if not isinstance(value,dict):return None
    name=value.get('username')
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.]{1,30}',name):return None
    return {'username':name,'external_id':str(value.get('id') or value.get('pk') or value.get('userid') or ''),'name':value.get('full_name') or name,'url':f'https://www.instagram.com/{name}/'}


def edges(value):
    if not isinstance(value,dict):return []
    return [item.get('node',{}) for item in value.get('edges') or [] if isinstance(item,dict)]


def timestamp(value):
    if value is None:return None
    try:
        if isinstance(value,(int,float)) or (isinstance(value,str) and value.isdigit()):return datetime.fromtimestamp(float(value),UTC).isoformat()
        return datetime.fromisoformat(str(value)).isoformat()
    except (ValueError,TypeError,OverflowError,OSError):return None


def normalize_post(node, relationships=None):
    relationships=relationships or {}
    phone=node.get('iphone_struct') or {}
    code=node.get('shortcode') or node.get('code') or phone.get('code')
    code=code if isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9_-]+',code) else None
    caption=node.get('caption')
    if isinstance(caption,dict):caption=caption.get('text')
    if not isinstance(caption,str):caption=next((x.get('text') for x in edges(node.get('edge_media_to_caption')) if isinstance(x.get('text'),str)), None)
    if caption is None:
        phone_caption=phone.get('caption') or {}
        caption=phone_caption.get('text','') if isinstance(phone_caption,dict) else str(phone_caption)
    graphql_children=edges(node.get('edge_sidecar_to_children'))
    phone_children=phone.get('carousel_media') or node.get('carousel_media') or []
    if graphql_children and len(graphql_children)==len(phone_children):
        raw_media=[{**mobile,**graphql} for mobile,graphql in zip(phone_children,graphql_children)]
    else:
        raw_media=graphql_children or phone_children or [{**phone,**node}]
    media=[];tagged=[]
    for index,child in enumerate(raw_media):
        video=child.get('is_video') or child.get('media_type')==2
        video_url=child.get('video_url') or next((v.get('url') for v in child.get('video_versions') or [] if v.get('url')),None)
        images=child.get('display_resources') or (child.get('image_versions2') or {}).get('candidates') or []
        image_url=child.get('display_url') or child.get('thumbnail_src') or next((v.get('src') or v.get('url') for v in images),None)
        dimensions=child.get('dimensions') or {}
        duration=child.get('video_duration')
        media.append({'external_id':str(child.get('id') or child.get('pk') or ''),'source_url':video_url or image_url,'thumbnail_url':image_url,
            'media_type':'video' if video or video_url else 'image','width':dimensions.get('width') or child.get('original_width'),'height':dimensions.get('height') or child.get('original_height'),
            'duration_ms':round(duration*1000) if isinstance(duration,(int,float)) else None,'accessibility_caption':child.get('accessibility_caption'),
            'image_resources':[{'url':v.get('src') or v.get('url'),'width':v.get('config_width') or v.get('width'),'height':v.get('config_height') or v.get('height')} for v in images if v.get('src') or v.get('url')],
            'video_resources':[{'url':v.get('src') or v.get('url'),'width':v.get('config_width') or v.get('width'),'height':v.get('config_height') or v.get('height')} for v in child.get('video_resources') or child.get('video_versions') or [] if v.get('src') or v.get('url')]})
        tagged_rows=edges(child.get('edge_media_to_tagged_user'))+((child.get('usertags') or {}).get('in') or [])
        for entry in tagged_rows:
            user=user_identity(entry.get('user'))
            if user:tagged.append({**user,'media_index':index+1,'x':entry.get('x'),'y':entry.get('y')})
    # GraphQL also exposes tags on the carousel parent.
    for entry in edges(node.get('edge_media_to_tagged_user'))+((phone.get('usertags') or {}).get('in') or []):
        user=user_identity(entry.get('user'))
        if user and not any(x['username']==user['username'] for x in tagged):tagged.append(user)
    comments=node.get('edge_media_to_parent_comment') or node.get('edge_media_to_comment') or {}
    interactions=[];previews={'commenters':[],'repliers':[],'likers':[]}
    def add_comment(comment,parent=None):
        author=user_identity(comment.get('owner') or comment.get('user'))
        if not author:return
        group='repliers' if parent else 'commenters'
        if len(previews[group])<5 and not any(x['username']==author['username'] for x in previews[group]):previews[group].append(author)
        cid=str(comment.get('id') or comment.get('pk') or hashlib.sha256(json.dumps(comment,sort_keys=True).encode()).hexdigest())
        interactions.append({'external_id':cid,'interaction_type':'reply' if parent else 'comment','author_external_id':author['external_id'] or 'username:'+author['username'],'author_name':author['username'],'content':{'text':comment.get('text'),'parent_comment_id':parent},'occurred_at':timestamp(comment.get('created_at') or comment.get('created_at_utc'))})
        for reply in edges(comment.get('edge_threaded_comments'))+list(comment.get('preview_child_comments') or [])+list(comment.get('answers') or []):
            if not parent:add_comment(reply,cid)
    for comment in edges(comments)+list(phone.get('preview_comments') or [])+list((relationships.get('comments') or {}).get('records') or []):add_comment(comment)
    like_data=node.get('edge_media_preview_like') or node.get('edge_liked_by') or {}
    for person in edges(like_data)+list(phone.get('facepile_top_likers') or [])+list((relationships.get('likes') or {}).get('profiles') or []):
        user=user_identity(person)
        if user and len(previews['likers'])<5 and not any(x['username']==user['username'] for x in previews['likers']):previews['likers'].append(user)
    mentions=list(dict.fromkeys(re.findall(r'(?<![\w@])@([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?)',caption)))
    for entry in relationships.get('caption_mentions') or []:
        name=entry if isinstance(entry,str) else entry.get('username') if isinstance(entry,dict) else None
        if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_.]{1,30}',name) and name not in mentions:mentions.append(name)
    for entry in relationships.get('tagged_users') or []:
        user=user_identity({'username':entry} if isinstance(entry,str) else entry)
        if user and not any(t['username']==user['username'] for t in tagged):tagged.append(user)
    location=node.get('location') or phone.get('location')
    if isinstance(location,dict):location={k:v for k,v in location.items() if v is not None and k!='profile_pic_url'}
    else:location=None
    return {'post_id':str(node.get('id') or node.get('pk') or ''),'shortcode':code,'permalink':f'https://www.instagram.com/p/{code}/' if code else None,
        'published_at':timestamp(node.get('date') or node.get('taken_at_timestamp') or node.get('taken_at') or phone.get('taken_at')),
        'text':caption,'media_type':'carousel' if len(raw_media)>1 or node.get('__typename')=='GraphSidecar' else ('video' if media and media[0]['media_type']=='video' else 'image'),
        'media':media,'location':location,'tagged_users':tagged,'caption_mentions':[{'username':name,'url':f'https://www.instagram.com/{name}/'} for name in mentions],
        'like_count':like_data.get('count',node.get('likes') if isinstance(node.get('likes'),int) else phone.get('like_count')),
        'comment_count':comments.get('count',node.get('comments') if isinstance(node.get('comments'),int) else phone.get('comment_count')),
        'interactions':list({(i['interaction_type'],i['external_id']):i for i in interactions}.values()),'people_preview':previews,'people_preview_is_complete':False,
        'interaction_collection':{key:{'policy':(relationships.get(key) or {}).get('policy'),'count':(relationships.get(key) or {}).get('count')} for key in ('comments','likes')}}


def read_post(path):
    """Read the matching collected sidecars; never guess an owner from names."""
    document=json.loads(path.read_text());node=document.get('node',document)
    code=node.get('shortcode')
    relationships={}
    if isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9_-]+',code):
        raw_path=path.with_name(f'post-{code}.raw-node.json')
        if raw_path.is_file():
            raw=json.loads(raw_path.read_text());raw_node=raw.get('node') or {}
            if raw.get('shortcode')!=code or (raw_node.get('id') and str(raw_node['id'])!=str(node.get('id'))):
                raise ValueError('Instagram raw sidecar identity mismatch')
            node={**raw_node,**node}
        sidecar=path.with_name(f'post-{code}.relationships.json')
        if sidecar.is_file():
            relationships=json.loads(sidecar.read_text())
            if relationships.get('shortcode')!=code:raise ValueError('Instagram relationship sidecar identity mismatch')
    return node,normalize_post(node,relationships)

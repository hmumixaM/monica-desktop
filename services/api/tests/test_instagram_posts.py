from app.instagram_posts import normalize_post


def test_carousel_metadata_tags_mentions_and_identity_previews():
    value={'id':'123','shortcode':'Ab_12','date':1700000000,'caption':'Hello @alice and @bob. Email a@b.com', '__typename':'GraphSidecar',
      'edge_sidecar_to_children':{'edges':[{'node':{'display_url':'https://cdn.test/a.jpg','is_video':False}},{'node':{'display_url':'https://cdn.test/b.jpg','is_video':True}}]},
      'iphone_struct':{'carousel_media':[{'pk':'c1','original_width':800,'original_height':600,'usertags':{'in':[{'user':{'pk':9,'username':'tagged'},'x':0.3,'y':0.4}]}},{'pk':'c2','video_versions':[{'url':'https://cdn.test/v.mp4','width':720,'height':1280}],'video_duration':2.5}], 'location':{'pk':5,'name':'Cafe','lat':1.2,'lng':3.4},'facepile_top_likers':[{'id':4,'username':'fan'}]},
      'edge_media_preview_like':{'count':0},'edge_media_to_parent_comment':{'count':2,'edges':[{'node':{'id':'c','text':'hi','owner':{'id':2,'username':'commenter'},'edge_threaded_comments':{'edges':[{'node':{'id':'r','text':'reply','owner':{'id':3,'username':'replyer'}}}]}}}]}}
    post=normalize_post(value)
    assert post['post_id']=='123' and post['permalink']=='https://www.instagram.com/p/Ab_12/'
    assert post['media_type']=='carousel' and post['published_at']
    assert post['media'][0]['external_id']=='c1' and post['media'][0]['width']==800
    assert post['media'][1]['source_url']=='https://cdn.test/v.mp4' and post['media'][1]['duration_ms']==2500
    assert [x['username'] for x in post['caption_mentions']]==['alice','bob']
    assert post['tagged_users'][0]['media_index']==1
    assert post['location']['pk']==5 and post['like_count']==0
    assert [x['interaction_type'] for x in post['interactions']]==['comment','reply']
    assert post['people_preview']['commenters'][0]['username']=='commenter'
    assert post['people_preview']['repliers'][0]['username']=='replyer'
    assert post['people_preview']['likers'][0]['username']=='fan'


def test_missing_details_are_not_fabricated():
    post=normalize_post({'id':'2','shortcode':'javascript:bad','iphone_struct':{'caption':{'text':'mobile caption'}}})
    assert post['permalink'] is None and post['text']=='mobile caption'
    assert post['like_count'] is None and post['location'] is None
    assert post['people_preview']=={'commenters':[],'repliers':[],'likers':[]}


def test_post_annotation_keeps_location_id_and_mentions():
    from app.event_metadata import normalize
    result=normalize('instagram','post',{'location':{'pk':8,'name':'Place','lat':10,'lng':20},'instagram_post':{'caption':'With @alice','post_id':'1','shortcode':'ABC'}})
    assert result['locations'][0]['location_id']=='8'
    assert result['locations'][0]['latitude']==10
    assert result['mentions'][0]['url']=='https://www.instagram.com/alice/'
    assert 'caption' not in result['post']


def test_relationship_sidecars_include_reply_identities_and_policies(tmp_path):
    import json

    from app.instagram_posts import read_post
    path=tmp_path/'2025_UTC_ABC.json';path.write_text(json.dumps({'node':{'id':'1','shortcode':'ABC','caption':'post'}}))
    relation={'shortcode':'ABC','tagged_users':['tagged'],'caption_mentions':['alice'],'comments':{'policy':'collected','records':[{'id':1,'owner':{'userid':2,'username':'commenter'},'answers':[{'id':3,'owner':{'userid':4,'username':'replier'}}]}]},'likes':{'policy':'partial_preview','profiles':[{'userid':5,'username':'fan'}]}}
    (tmp_path/'post-ABC.relationships.json').write_text(json.dumps(relation))
    _,post=read_post(path)
    assert post['people_preview']['repliers'][0]['external_id']=='4'
    assert post['people_preview']['likers'][0]['username']=='fan'
    assert post['tagged_users'][0]['username']=='tagged'
    assert post['caption_mentions'][0]['username']=='alice'
    assert post['interaction_collection']['likes']['policy']=='partial_preview'

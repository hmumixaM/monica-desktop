"""Monica canvas contract projected from resolved source identities and live facts."""
import math
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import text

from .community_names import community_labels
from .graph_layout import lay_out


def graph_community_members(nodes) -> dict[int, list[str]]:
    members: dict[int, list[str]] = defaultdict(list)
    for node in nodes:
        members[node['community']].append(node['profile_id'])
    return members


def graph_community_keys(nodes) -> dict[int, str]:
    """A community keeps its key while its first member (by id) stays in it."""
    return {index: f"graph:{min(ids)}" for index, ids in sorted(graph_community_members(nodes).items())}


def graph_projection(db, raw_root, limit=900, focus=None):
    limit=min(max(limit,50),2000)
    params={'limit':limit, 'focus':str(focus) if focus else None}
    # Resolve a person's indexed interaction IDs before joining recipients.
    # Filtering the joined graph afterwards reads every person's interactions
    # on a cold NAS, even when opening a small personal circle.
    # Read each candidate's narrow graph columns once. A UNION of whole graph
    # tuples removes overlaps without a second random lookup of every row ID.
    columns='s.id,s.post_id,s.author_profile_id,s.interaction_type,s.occurred_at,s.target_external_id,s.ref_author_external_id'
    candidates=f"""focus_accounts AS MATERIALIZED (
        SELECT external_id FROM identities WHERE profile_id=CAST(:focus AS uuid)
          AND provider='wechat' AND status NOT IN ('invalid','merged')
      ), relevant_interactions AS MATERIALIZED (
        SELECT {columns} FROM social_interactions s WHERE s.provider='wechat' AND NOT s.is_deleted
          AND s.author_profile_id=CAST(:focus AS uuid)
        UNION
        SELECT {columns} FROM social_posts p JOIN social_interactions s ON s.post_id=p.id
          WHERE p.profile_id=CAST(:focus AS uuid) AND s.provider='wechat' AND NOT s.is_deleted
        UNION
        SELECT {columns} FROM focus_accounts f JOIN social_interactions s ON s.target_external_id=f.external_id
          WHERE s.provider='wechat' AND s.interaction_type='mention' AND NOT s.is_deleted
        UNION
        SELECT {columns} FROM focus_accounts f JOIN social_interactions s ON s.ref_author_external_id=f.external_id
          WHERE s.provider='wechat' AND s.interaction_type='reply' AND NOT s.is_deleted
      ),""" if focus else ''
    interactions='relevant_interactions' if focus else 'social_interactions'
    active_filter='' if focus else "AND s.provider='wechat' AND NOT s.is_deleted"
    rows=db.execute(text(f"""
        WITH {candidates} interaction_edges AS (
        SELECT s.author_profile_id AS a,
            CASE WHEN s.interaction_type='mention' THEN recipient.profile_id ELSE p.profile_id END AS b,
            CASE WHEN s.interaction_type='reply' THEN 'comment' ELSE s.interaction_type END AS kind,s.occurred_at
        FROM {interactions} s JOIN social_posts p ON p.id=s.post_id
        LEFT JOIN identities recipient ON recipient.provider='wechat'
            AND recipient.external_id=s.target_external_id AND recipient.status NOT IN ('invalid','merged')
        WHERE s.author_profile_id IS NOT NULL {active_filter}
        UNION ALL
        SELECT s.author_profile_id,recipient.profile_id,'reply',s.occurred_at
        FROM {interactions} s JOIN identities recipient ON recipient.provider='wechat'
            AND recipient.external_id=s.ref_author_external_id AND recipient.status NOT IN ('invalid','merged')
        WHERE s.interaction_type='reply' AND s.author_profile_id IS NOT NULL {active_filter}
        ), edge_counts AS MATERIALIZED (
        SELECT e.a,e.b,e.kind,count(*) AS n,max(e.occurred_at) AS last
        FROM interaction_edges e
        WHERE e.a<>e.b AND (CAST(:focus AS uuid) IS NULL OR e.a=CAST(:focus AS uuid) OR e.b=CAST(:focus AS uuid))
        GROUP BY e.a,e.b,e.kind
        )
        SELECT e.a,e.b,e.kind AS interaction_type,e.n,e.last
        FROM edge_counts e
        JOIN profiles a ON a.id=e.a AND a.archived_at IS NULL AND a.profile_type='person'
        JOIN profiles b ON b.id=e.b AND b.archived_at IS NULL AND b.profile_type='person'
        ORDER BY e.n DESC LIMIT :limit
    """),params).mappings().all()
    pairs={}; node_ids=set()
    for row in rows:
        a,b=sorted([str(row['a']),str(row['b'])]);node_ids.update((a,b))
        edge=pairs.setdefault((a,b),{'ab':[0,0,0,0],'ba':[0,0,0,0],'group':0,'direct':0})
        index={'like':0,'comment':1,'reply':2,'mention':3}.get(row['interaction_type'])
        if index is not None:
            edge['ab' if str(row['a'])==a else 'ba'][index]+=row['n']
            edge['direct']+=row['n']
            if row['last']:
                edge['last']=max(edge.get('last',''),row['last'].date().isoformat())
    if focus:node_ids.add(str(focus))
    focus_members=[]
    if focus:
        # The checkbox can reveal real peers connected solely through a shared
        # group. Every displayed pair touches the focus, so only its groups
        # can be shared. Reuse this narrow membership set for edge details.
        focus_members=db.execute(text("""WITH focus_groups AS MATERIALIZED (
            SELECT group_profile_id FROM group_memberships
            WHERE person_profile_id=CAST(:focus AS uuid) AND left_at IS NULL
            ), small_groups AS MATERIALIZED (
            SELECT m.group_profile_id FROM focus_groups f
            JOIN group_memberships m ON m.group_profile_id=f.group_profile_id
            WHERE m.left_at IS NULL GROUP BY m.group_profile_id HAVING count(*)<=80)
            SELECT m.group_profile_id,m.person_profile_id FROM small_groups sizes
            JOIN group_memberships m ON m.group_profile_id=sizes.group_profile_id AND m.left_at IS NULL
            JOIN profiles g ON g.id=m.group_profile_id AND g.profile_type='group' AND g.archived_at IS NULL
            JOIN profiles p ON p.id=m.person_profile_id AND p.profile_type='person' AND p.archived_at IS NULL
            """),params).all()
        peer_counts=defaultdict(int)
        for _group,peer in focus_members:
            if str(peer)!=str(focus):peer_counts[str(peer)]+=1
        peers=sorted(peer_counts.items(),key=lambda item:(-item[1],item[0]))[:limit]
        for peer,shared in peers:
            a,b=sorted((str(focus),str(peer)));node_ids.update((a,b))
            pairs.setdefault((a,b),{'ab':[0,0,0,0],'ba':[0,0,0,0],'group':shared,'direct':0})
    # Explicit relationships also belong in the graph, but invalid legacy group
    # edges never do. Group membership comes from its single authoritative table.
    relations=db.execute(text("""SELECT e.from_profile_id a,e.to_profile_id b,t.name
        FROM relationship_edges e JOIN relationship_types t ON t.id=e.relationship_type_id
        JOIN profiles a ON a.id=e.from_profile_id AND a.archived_at IS NULL AND a.profile_type='person'
        JOIN profiles b ON b.id=e.to_profile_id AND b.archived_at IS NULL AND b.profile_type='person'
        WHERE e.evidence_kind='relationship' AND a.id<>b.id AND lower(t.name) NOT SIMILAR TO '%(group|member|owner|群)%'
        AND (CAST(:focus AS uuid) IS NULL OR a.id=CAST(:focus AS uuid) OR b.id=CAST(:focus AS uuid))
        ORDER BY e.created_at DESC LIMIT :limit"""),params).mappings()
    for row in relations:
        a,b=sorted([str(row['a']),str(row['b'])]);node_ids.update((a,b))
        edge=pairs.setdefault((a,b),{'ab':[0,0,0,0],'ba':[0,0,0,0],'group':0,'direct':0})
        edge.setdefault('relationship_types',[]).append(row['name'])
        edge['direct']+=1
    # Photo annotations remain in galleries; they do not establish relationship edges.
    shared_groups=defaultdict(set)
    shared_audience=defaultdict(set)
    if node_ids:
        members=focus_members if focus else db.execute(text("""WITH small_groups AS MATERIALIZED (
            SELECT group_profile_id FROM group_memberships WHERE left_at IS NULL
            GROUP BY group_profile_id HAVING count(*)<=80)
            SELECT m.group_profile_id,m.person_profile_id FROM group_memberships m
            JOIN small_groups sizes ON sizes.group_profile_id=m.group_profile_id
            JOIN profiles g ON g.id=m.group_profile_id AND g.profile_type='group' AND g.archived_at IS NULL
            JOIN profiles p ON p.id=m.person_profile_id AND p.profile_type='person' AND p.archived_at IS NULL
            WHERE m.left_at IS NULL AND person_profile_id=ANY(CAST(:ids AS uuid[]))"""),{'ids':list(node_ids)}).all()
        by_person=defaultdict(set)
        for group,person in members:by_person[str(person)].add(str(group))
        for (a,b),edge in pairs.items():
            shared=by_person[a]&by_person[b];edge['group']=len(shared)
            shared_groups[a].update(shared);shared_groups[b].update(shared)
        audience=defaultdict(set)
        # A personal graph contains only pairs touching its focus. A shared
        # audience member must therefore have interacted with that focus.
        # Avoid reading every neighbor's unrelated interactions on a cold NAS.
        focus_audience="""audience_posts AS MATERIALIZED (
            SELECT id,profile_id FROM social_posts WHERE profile_id=ANY(CAST(:ids AS uuid[]))
        ), focus_audience AS MATERIALIZED (
            SELECT DISTINCT s.author_profile_id
            FROM social_posts p JOIN social_interactions s ON s.post_id=p.id
            WHERE p.profile_id=CAST(:focus AS uuid) AND s.provider='wechat'
              AND NOT s.is_deleted AND s.author_profile_id<>p.profile_id
        ),""" if focus else ''
        audience_posts='audience_posts' if focus else 'social_posts'
        audience_filter='AND s.author_profile_id IN (SELECT author_profile_id FROM focus_audience)' if focus else ''
        for author,owner in db.execute(text(f"""WITH {focus_audience} audience AS MATERIALIZED (
            SELECT DISTINCT s.author_profile_id,p.profile_id
            FROM social_interactions s JOIN {audience_posts} p ON p.id=s.post_id
            WHERE s.provider='wechat' AND NOT s.is_deleted AND p.profile_id=ANY(CAST(:ids AS uuid[]))
            AND s.author_profile_id<>p.profile_id {audience_filter})
            SELECT a.author_profile_id,a.profile_id FROM audience a
            JOIN profiles author ON author.id=a.author_profile_id AND author.archived_at IS NULL AND author.profile_type='person'
            """),{'ids':list(node_ids),'focus':str(focus) if focus else None}):
            audience[str(owner)].add(str(author))
        for (a,b),edge in pairs.items():
            shared=(audience[a]&audience[b])-{a,b}
            edge['co']=len(shared)
            shared_audience[a].update(shared);shared_audience[b].update(shared)
    nodes=[]; index={}
    if node_ids:
        profiles=db.execute(text("""SELECT p.id,p.display_name,p.profile_type,
            (SELECT external_id FROM identities i WHERE i.profile_id=p.id AND i.provider='wechat'
             AND i.status NOT IN ('invalid','merged') ORDER BY i.first_seen_at,i.id LIMIT 1) AS wxid
            FROM profiles p WHERE p.id=ANY(CAST(:ids AS uuid[])) AND p.archived_at IS NULL ORDER BY p.id"""),{'ids':list(node_ids)}).mappings().all()
        for row in profiles:
            pid=str(row['id']); index[pid]=len(nodes)
            nodes.append({'u':row['wxid'] or pid,'name':row['display_name'],'hash':pid,'profile_id':pid,
                'known':True,'profile_type':row['profile_type'],'x':0,'y':0,'community':0,'deg':0,'partners':0,'facts':[0,0,0,0,len(shared_audience[pid]),len(shared_groups[pid])]})
    edges=[]
    for (a,b),edge in pairs.items():
        if a not in index or b not in index:continue
        edge={**edge,'a':index[a],'b':index[b],'w':edge['direct'],'combined':edge['direct']+edge['group'],
              'group_weight':edge['group'],'reciprocity':int(any(edge['ab']) and any(edge['ba']))}
        edges.append(edge)
        for key in ('a','b'):
            node=nodes[edge[key]];node['deg']+=1
            node['partners']+=1
            if edge.get('last'):node['last']=max(node.get('last',''),edge['last'])
            for i in range(4):node['facts'][i]+=edge['ab'][i]+edge['ba'][i]
    lay_out(nodes,edges)
    names = community_labels(db)
    communities = [{'index': index, 'key': key, **names.get(key, {})} for index, key in graph_community_keys(nodes).items()]
    return {'nodes':nodes,'edges':edges,'version':2,'communities':communities,'generated_at':datetime.now(UTC).isoformat(),'source':'resolved database interactions and confirmed relationships',
            'monica_mapping':{'mapped_nodes':len(nodes),'source_nodes':len(nodes),'match_rate':1 if nodes else 0},
            'stats':{'nodes':len(nodes),'edges':len(edges)},'scope':{'limit':limit,'focus':focus}}


def circle_projection(db, raw_root, profile_id):
    payload = graph_projection(db, raw_root, limit=2000, focus=str(profile_id))
    center = next((n for n in payload['nodes'] if n['profile_id'] == str(profile_id)), None)
    if not center:
        return {'center': {'id':str(profile_id),'label':'当前联系人','url':f'/profiles/{profile_id}'},'neighbors':[],'maximumDirect':0,'maximumWeight':0}
    ci = payload['nodes'].index(center)
    neighbors=[]
    for edge in payload['edges']:
        if ci not in (edge['a'],edge['b']):continue
        peer=payload['nodes'][edge['b'] if edge['a']==ci else edge['a']]
        outward,inward=(edge['ab'],edge['ba']) if edge['a']==ci else (edge['ba'],edge['ab'])
        counts=[outward[i]+inward[i] for i in range(4)]
        direct=sum(c*w for c,w in zip(counts,[1,3,4,12]))
        if not direct and not edge['group']:continue
        outgoing=sum(outward); incoming=sum(inward)
        neighbors.append({'id':peer['profile_id'],'label':peer['name'],'url':f"/profiles/{peer['profile_id']}",
            'x':0,'y':0,'out':outward,'in':inward,'direct':direct,'weight':direct+edge['group'],
            'group':edge['group'],'co':edge.get('co',0),'groupOnly':not direct,'last':edge.get('last'),
            'reciprocity':2*min(outgoing,incoming)/(outgoing+incoming) if outgoing+incoming else 0,
            **dict(zip(['like','comment','reply','mention'],counts))})
    neighbors.sort(key=lambda n:(-n['direct'],-n['group'],n['id']))
    for i,n in enumerate(neighbors):
        angle=i*math.pi*(3-math.sqrt(5));n['x']=.7*math.cos(angle);n['y']=.7*math.sin(angle)
    return {'center':{'id':center['profile_id'],'label':center['name'],'url':f'/profiles/{profile_id}'},'neighbors':neighbors,
            'maximumDirect':max((n['direct'] for n in neighbors),default=0),'maximumWeight':max((n['weight'] for n in neighbors),default=0)}

"""Readable projections retain source evidence and never merge people by name."""
import re
from urllib.parse import unquote, urlsplit

ALIASES = {
    'LinkedIn': 'linkedin.profile_url', 'LinkedIn About': 'linkedin.about',
    '职业简介': 'linkedin.headline', 'LinkedIn Location': 'linkedin.location',
    'LinkedIn 连接日期': 'linkedin.connected_on', 'LinkedIn 共同好友数': 'linkedin.mutual_count',
    'Instagram': 'instagram.profile_url', 'Instagram Bio': 'instagram.biography',
    'Instagram 简介': 'instagram.biography', 'Instagram 粉丝数': 'instagram.followers_count',
    'Instagram 关注数': 'instagram.followees_count', 'Instagram 帖子数': 'instagram.media_count',
    '微信号': 'wechat.alias', '微信备注': 'wechat.remark', '微信昵称': 'wechat.nickname',
    '微信群内显示名': 'wechat.group_nicknames', '微信首次群内出现': 'wechat.first_group_seen',
    '微信最近群内出现': 'wechat.last_group_seen',
    '微信个性签名': 'wechat.signature', '微信好友来源': 'wechat.add_source', '微信资料地区': 'wechat.region',
}


def clean_text(value):
    if isinstance(value, list):
        return '\n'.join(dict.fromkeys(clean_text(v) for v in value)) if all(isinstance(v, str) for v in value) else value
    if isinstance(value, str):
        return value.replace('\\r\\n', '\n').replace('\\n', '\n').strip()
    return value


def identity_key(provider, external_id, profile_url=None, username=None):
    """Canonical account key from a provider URL or identifier, never its label."""
    if provider not in ('linkedin', 'instagram'):
        return str(external_id)
    for value in (profile_url, external_id):
        if not value or '://' not in value:
            continue
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower().removeprefix('www.')
        parts = [unquote(p) for p in parsed.path.strip('/').split('/')]
        if provider == 'linkedin' and host.endswith('linkedin.com') and (host == 'linkedin.com' or host.endswith('.linkedin.com')) and len(parts) == 2 and parts[0] == 'in':
            return parts[1].casefold()
        if provider == 'instagram' and host == 'instagram.com' and len(parts) == 1 and parts[0] not in ('p', 'reel', 'stories'):
            return parts[0].casefold()
    value = str(external_id or '').removeprefix('username:').removeprefix('@')
    if re.fullmatch(r'[A-Za-z0-9_.-]+', value):
        return value.casefold()
    if provider == 'linkedin' and re.fullmatch(r'urn:li:(?:fsd_profile|member):[A-Za-z0-9_-]+', value):
        return value
    # LinkedIn vanity names may use any script and "~"; spaces never occur in one.
    if provider == 'linkedin' and re.fullmatch(r'[\w.~-]+', unquote(value)):
        return unquote(value).casefold()
    return None


def present_identities(identities):
    result = {}
    for row in identities:
        item = dict(row)
        provider = item['provider']
        if provider == 'monica':
            continue
        key = identity_key(provider, item['external_id'], item.get('profile_url'), item.get('username'))
        if not key:
            continue
        # A numeric Instagram id and its username can coexist on one account.
        # Collapse only if that id row explicitly supplies that username.
        if provider == 'instagram' and item.get('username'):
            key = item['username'].removeprefix('@').casefold()
        if provider == 'linkedin':
            item['profile_url'] = f'https://www.linkedin.com/in/{key}' if not key.startswith('urn:') else None
        elif provider == 'instagram':
            item['profile_url'] = f'https://www.instagram.com/{key}/' if not key.isdigit() else None
        elif provider.startswith('wechat'):
            item['profile_url'] = None
        unique = (provider, key)
        if unique not in result:
            result[unique] = item
    return list(result.values())


def presentation(fields, facts):
    values = {}
    custom = next((f['value'] for f in fields if f['field_key'] == 'monica.custom_fields'), {})
    if isinstance(custom, dict):
        for label, value in custom.items():
            if key := ALIASES.get(label):
                values[key] = clean_text(value)
    for field in fields:
        if field['field_key'] != 'monica.custom_fields':
            values[field['field_key']] = clean_text(field['value'])
    accounts={}
    for key,value in values.items():
        if key.startswith('instagram.accounts.'):
            _,_,account,field=key.split('.',3)
            accounts.setdefault(account,{'account_id':account})[field]=value
    sources = {}
    for source in ('wechat', 'linkedin', 'instagram'):
        sources[source] = {key.split('.', 1)[1]: value for key, value in values.items()
                           if key.startswith(source + '.') and value not in (None, '', [])
                           and not re.search(r'snapshot|stats|history|summary|partial|hash|raw|avatar|pic_url|sync|cover|mutuals|accounts[.]', key)}
    if accounts:
        ordered=sorted(accounts.values(),key=lambda a:str(a.get('username','')))
        sources['instagram'].update(ordered[0])
    # Same value imported through two source paths is one displayed fact. Real
    # revisions stay untouched; distinct jobs at one company retain their dates.
    seen = set()
    unique_facts = []
    for row in facts:
        value = row['value']
        if isinstance(value, dict):
            meaningful = {key: value[key] for key in ('title','position_title','degree','name','institution_name','school_name','start_date','end_date','start','end','location','address','description') if value.get(key)}
            signature = repr(sorted(meaningful.items())) if meaningful else repr(value)
        else:
            signature = str(clean_text(value))
        key = (row['fact_type'], row['current_source_type'], row.get('company_name'), row.get('school_name'), signature)
        if key not in seen:
            seen.add(key)
            unique_facts.append(dict(row))
    unique_facts.sort(key=lambda f: str(f['value'].get('start_date', '')) if isinstance(f['value'], dict) else '', reverse=True)
    return {'sources': sources, 'facts': unique_facts, 'instagram_accounts':list(accounts.values())}

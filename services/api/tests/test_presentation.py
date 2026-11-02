from app.profile_presentation import identity_key, present_identities, presentation


def test_linkedin_fields_are_not_accounts_and_url_alias_is_one_account():
    assert identity_key('linkedin','26 Aug 2023','26 Aug 2023') is None
    assert identity_key('linkedin','Pittsburgh, Pennsylvania, United States') is None
    rows=[{'id':str(i),'provider':'linkedin','external_id':key,'profile_url':url}
          for i,(key,url) in enumerate([('han-bao','https://www.linkedin.com/in/han-bao/'),
              ('https://www.linkedin.com/in/han-bao','https://www.linkedin.com/in/han-bao'),('26 Aug 2023','26 Aug 2023')])]
    assert len(present_identities(rows))==1
    assert identity_key('linkedin','https://evil-linkedin.com/in/alice') is None


def test_linkedin_vanity_names_in_any_script_are_accounts():
    assert identity_key('linkedin','子江-杨-917282202')=='子江-杨-917282202'
    assert identity_key('linkedin','%E5%AD%90%E6%B1%9F-%E6%9D%A8-917282202')=='子江-杨-917282202'
    assert identity_key('linkedin','Juan-José-Rodríguez-5735a9a4')=='juan-josé-rodríguez-5735a9a4'
    assert identity_key('linkedin','~sh')=='~sh'
    assert identity_key('linkedin','子江 杨') is None
    assert identity_key('instagram','子江') is None


def test_profile_bios_preserve_platforms_and_deduplicate_only_equal_facts():
    fields=[{'field_key':'monica.custom_fields','value':{'LinkedIn About':['A\\nB'],'微信个性签名':['微信简介'],'Instagram 简介':['照片简介']}},
            {'field_key':'linkedin.about','value':'A\nB'}]
    base={'fact_type':'employment','current_source_type':'linkedin','company_name':'Company','school_name':None}
    facts=[{**base,'value':{'title':'Engineer','start_date':'2020'}},{**base,'value':{'title':'Engineer','start_date':'2020'}},{**base,'value':{'title':'Engineer','start_date':'2024'}}]
    out=presentation(fields,facts)
    assert out['sources']['linkedin']['about']=='A\nB'
    assert out['sources']['instagram']['biography']=='照片简介'
    assert out['sources']['wechat']['signature']=='微信简介'
    assert len(out['facts'])==2


def test_source_coordinates_reverse_only_chatlog_xml_slots():
    from app.location_points import _build_location_summary, _location_point

    for raw,expected in [
        ({'latitude':'7.61780024','longitude':'47.4845009'},(47.4845009,7.61780024)),
        ({'latitude':'-4.15626383','longitude':'55.9204254'},(55.9204254,-4.15626383)),
        ({'latitude':'-122.4194','longitude':'37.7749'},(37.7749,-122.4194)),
        ({'latitude':'121.471212','longitude':'31.152393'},(31.152393,121.471212)),
    ]:
        assert _location_point(raw,'chatlog_sns_xml')==expected
    assert _location_point({'lat':47.4845,'lon':7.6178})==(47.4845,7.6178)
    assert _location_point({'latitude':37.7749,'longitude':-122.4194})==(37.7749,-122.4194)
    assert _location_point({'latitude':0,'longitude':0},'chatlog_sns_xml') is None
    points=_build_location_summary([{'provider':'wechat','location':{'latitude':'7.61780024','longitude':'47.4845009','poiName':'Dornach'},'coordinate_format':'chatlog_sns_xml'}])
    assert 'mlat=47.4845009&mlon=7.61780024' in points[0]['map_url']


def test_legacy_graph_indexes_are_never_imported_as_wechat_accounts(tmp_path,monkeypatch):
    import json

    from app.full_migration import load_moments_and_relationships
    (tmp_path/'moments_graph.json').write_text(json.dumps({'nodes':[],'edges':[{'a':418,'b':372,'like':7}]}))
    class Database:
        def commit(self):pass
    monkeypatch.setattr('app.full_migration.add_relation',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Graph index became a person relationship')))
    load_moments_and_relationships(Database(),tmp_path,{'418':'person_a','372':'person_b'})


def test_city_reference_does_not_guess_ambiguous_or_country_only_locations():
    from app.location_atlas import resolve_city
    assert resolve_city('Springfield','US') is None
    assert resolve_city('United States') is None
    assert resolve_city('San Francisco Bay Area') is None
    pittsburgh=resolve_city('Pittsburgh, Pennsylvania, United States')
    assert 40<pittsburgh['latitude']<41 and -80<pittsburgh['longitude']<-79
    assert resolve_city('芝加哥','US','Illinois')['geoname_id']==4887398


def test_instagram_mobile_archive_location_is_retained_without_transposing():
    from app.location_points import _location_point
    from app.reconcile_sources import instagram_location
    location=instagram_location({'location':None,'iphone_struct':{'location':{'name':'San Jose','lat':37.3293048,'lng':-121.8875475,'profile_pic_url':None}}})
    assert location=={'name':'San Jose','lat':37.3293048,'lng':-121.8875475}
    assert _location_point(location)==(37.3293048,-121.8875475)


def test_city_reference_keeps_simultaneous_reader_connections_separate(tmp_path,monkeypatch):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app import city_reference
    path=tmp_path/'cities.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE cities(geonameid INTEGER PRIMARY KEY,name TEXT,countrycode TEXT,admin1code TEXT,latitude REAL,longitude REAL);
            CREATE TABLE names(name TEXT,city_id INTEGER);
            INSERT INTO cities VALUES(1,'Pittsburgh','US','PA',40.44062,-79.99589),(2,'Dornach','CH','SO',47.48,7.61);
            INSERT INTO names VALUES('pittsburgh',1),('dornach',2);''')
    monkeypatch.setattr(city_reference,'REFERENCE',path)
    barrier=Barrier(8)
    def read(index):
        connection=city_reference.reader();barrier.wait()
        # Each worker sees the shared immutable snapshot through its own
        # cursor connection; no worker may turn the reference into a writer.
        assert connection.execute('PRAGMA database_list').fetchone()['file']==''
        try:
            connection.execute("DELETE FROM names")
        except sqlite3.OperationalError as error:
            assert 'readonly' in str(error)
        else:
            raise AssertionError('City reference permitted a write')
        for _ in range(100):
            name='pittsburgh' if index%2 else 'dornach'
            row=city_reference.cities_named(name)[0]
            assert row['countrycode']==('US' if index%2 else 'CH')
            assert row['latitude']==(40.44062 if index%2 else 47.48)
        return id(connection)
    with ThreadPoolExecutor(max_workers=8) as pool:connections=list(pool.map(read,range(8)))
    assert len(set(connections))==8
    with sqlite3.connect(path) as disk:
        assert disk.execute('SELECT count(*) FROM names').fetchone()[0]==2

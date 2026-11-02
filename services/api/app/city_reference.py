"""A build-time index of GeoNames; no geocoding calls or personal data."""
import json
import sqlite3
import threading
import unicodedata
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

REFERENCE=Path(__file__).with_name('city-reference.sqlite3')
_readers=threading.local()
_reference_lock=threading.Lock()
_reference_path=None
_reference_uri=None
_reference_anchor=None


def memory_reference():
    """Copy the small immutable index once, retaining one shared page cache.

    A cold atlas needs hundreds of exact names. On the NAS their random disk
    reads dominate the request. SQLite backup reads the bundled index in page
    order; that cost remains part of the first request, with no startup warmup.
    Separate thread connections share these read-only pages, not their cursors.
    """
    global _reference_path,_reference_uri,_reference_anchor
    with _reference_lock:
        if _reference_path!=REFERENCE:
            uri=f'file:city-reference-{uuid4().hex}?mode=memory&cache=shared'
            anchor=sqlite3.connect(uri,uri=True,check_same_thread=False)
            try:
                with closing(sqlite3.connect(REFERENCE.as_uri()+'?mode=ro&immutable=1',uri=True)) as disk:
                    disk.backup(anchor)
                anchor.execute('PRAGMA query_only=ON')
            except BaseException:
                anchor.close()
                raise
            previous=_reference_anchor
            _reference_path,_reference_uri,_reference_anchor=REFERENCE,uri,anchor
            if previous:previous.close()
        return _reference_uri


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC',str(value or '')).casefold().split())


def reader():
    if not REFERENCE.is_file():return None
    # FastAPI runs simultaneous map requests in different worker threads.
    # Each gets its own SQLite cursor/statement cache; a shared connection can
    # race while SQLite Row column metadata is being consumed by another query.
    if getattr(_readers,'path',None)!=REFERENCE:
        previous=getattr(_readers,'connection',None)
        if previous:previous.close()
        _readers.connection=sqlite3.connect(memory_reference(),uri=True)
        _readers.connection.execute('PRAGMA query_only=ON')
        _readers.connection.row_factory=sqlite3.Row
        _readers.path=REFERENCE
    return _readers.connection


@lru_cache(maxsize=1)
def administrative_names():
    connection=reader()
    if connection:
        countries=dict(connection.execute('SELECT name,code FROM countries'))
        states=dict(connection.execute('SELECT name,code FROM states'))
    else:
        import geonamescache
        cache=geonamescache.GeonamesCache();countries={}
        for code,country in cache.get_countries().items():
            for name in (code,country['iso3'],country['name']):countries[normalized(name)]=code
        states={normalized(v['name']):k for k,v in cache.get_us_states().items()}
    for name,code in {'中国':'CN','美国':'US','英国':'GB','香港':'HK','加拿大':'CA','澳大利亚':'AU','新加坡':'SG','日本':'JP','韩国':'KR','法国':'FR','德国':'DE','瑞士':'CH','uk':'GB'}.items():countries[normalized(name)]=code
    return countries,states


@lru_cache(maxsize=1)
def development_names():
    # Source checkouts can run without generated assets. Production images
    # always contain the index, so first-page requests never parse all cities.
    import geonamescache
    names={}
    for city in geonamescache.GeonamesCache().get_cities().values():
        for name in {normalized(v) for v in (city['name'],*city['alternatenames'])}:
            names.setdefault(name,[]).append(city)
    return names


def cities_named(name):
    connection=reader()
    if connection:
        return connection.execute('''SELECT c.* FROM names n JOIN cities c ON c.geonameid=n.city_id
            WHERE n.name=?''',(name,)).fetchall()
    return development_names().get(name,[])


def build(path=REFERENCE):
    import geonamescache
    cache=geonamescache.GeonamesCache();cities=cache.get_cities()
    temporary=path.with_suffix('.building');temporary.unlink(missing_ok=True)
    with sqlite3.connect(temporary) as db:
        db.executescript('''CREATE TABLE cities(geonameid INTEGER PRIMARY KEY,name TEXT,countrycode TEXT,
            admin1code TEXT,latitude REAL,longitude REAL);
            CREATE TABLE names(name TEXT,city_id INTEGER,PRIMARY KEY(name,city_id)) WITHOUT ROWID;
            CREATE TABLE countries(name TEXT PRIMARY KEY,code TEXT) WITHOUT ROWID;
            CREATE TABLE states(name TEXT PRIMARY KEY,code TEXT) WITHOUT ROWID;''')
        db.executemany('INSERT INTO cities VALUES(?,?,?,?,?,?)',[
            (c['geonameid'],c['name'],c['countrycode'],c['admin1code'],c['latitude'],c['longitude']) for c in cities.values()])
        db.executemany('INSERT INTO names VALUES(?,?)',[(name,c['geonameid']) for c in cities.values()
            for name in {normalized(v) for v in (c['name'],*c['alternatenames'])}])
        countries={normalized(name):code for code,c in cache.get_countries().items() for name in (code,c['iso3'],c['name'])}
        db.executemany('INSERT INTO countries VALUES(?,?)',countries.items())
        db.executemany('INSERT INTO states VALUES(?,?)',[(normalized(v['name']),k) for k,v in cache.get_us_states().items()])
        total=db.execute('SELECT count(*) FROM names').fetchone()[0]
        db.execute('ANALYZE');db.commit()
    temporary.replace(path)
    return {'reference_cities':len(cities),'reference_names':total}


if __name__=='__main__':print(json.dumps(build()))

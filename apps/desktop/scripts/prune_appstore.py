"""Prepare the explicitly reduced App Store edition before signing.

Only a build copy is modified. The standalone runtime keeps its collectors and
face processing dependencies. Python 3.12 and packaging are build requirements.
"""
import importlib.metadata
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def requirement_closure(distributions, requirements):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    known = {canonicalize_name(d.metadata['Name']): d for d in distributions}
    selected, pending = set(), list(requirements)
    processed = set()
    while pending:
        spec = Requirement(pending.pop())
        name = canonicalize_name(spec.name)
        if name not in known:
            raise ValueError('Missing runtime dependency: ' + name)
        extras = frozenset(spec.extras)
        if (name, extras) in processed:
            continue
        processed.add((name, extras))
        distribution = known[name]
        if spec.specifier and distribution.version not in spec.specifier:
            raise ValueError('Runtime dependency does not satisfy ' + str(spec))
        selected.add(name)
        for entry in distribution.requires or []:
            requirement = Requirement(entry)
            if not requirement.marker or any(requirement.marker.evaluate({'extra': extra})
                                              for extra in ({''} | set(extras))):
                pending.append(str(requirement))
    return known, selected


def prune(runtime):
    runtime = runtime.resolve()
    site = runtime / 'python/lib/python3.12/site-packages'
    # packaging is pure Python and already present in the build input.
    sys.path.insert(0, str(site))
    import tomllib
    requirements = tomllib.loads((ROOT / 'services/api/pyproject.toml').read_text())['project']['dependencies']
    # The complete GeoNames index is generated before packaging; JSON source
    # datasets are needed only for regenerating that immutable database.
    requirements = [r for r in requirements if not r.startswith('geonamescache')]
    requirements = [r.replace('uvicorn[standard]', 'uvicorn') for r in requirements]
    # Keep timezone rules available inside the application container too.
    requirements.append('tzdata')
    reference = runtime / 'api/app/city-reference.sqlite3'
    with sqlite3.connect(reference.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
        if connection.execute('SELECT count(*) FROM cities').fetchone()[0] < 1000:
            raise ValueError('Generated city reference is incomplete')
        if connection.execute('SELECT count(*) FROM countries').fetchone()[0] < 200:
            raise ValueError('Generated country reference is incomplete')
    known, selected = requirement_closure(importlib.metadata.distributions(path=[str(site)]), requirements)
    protected = {Path(d.locate_file(f)).resolve() for n, d in known.items() if n in selected for f in d.files or []}
    removed_packages = []
    for name, distribution in known.items():
        if name in selected:
            continue
        removed_packages.append(name)
        for entry in distribution.files or []:
            path = Path(distribution.locate_file(entry))
            resolved = path.resolve()
            if not resolved.is_relative_to(runtime / 'python'):
                raise ValueError('Python package record escapes the runtime')
            if resolved not in protected and (path.is_file() or path.is_symlink()):
                path.unlink()
    for folder in sorted(site.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        if folder.is_dir() and not folder.is_symlink() and not any(folder.iterdir()):
            folder.rmdir()
    for name in ('browsers', 'collectors', 'faces', 'models', 'node/include', 'node/lib', 'node/share',
                 'python/include', 'python/share', 'python/insightface', 'python/lib/python3.12/tkinter',
                 'python/lib/libtk8.6.dylib', 'python/lib/libtcl8.6.dylib',
                 'python/lib/python3.12/lib-dynload/_tkinter.cpython-312-darwin.so',
                 'postgres/share/doc', 'postgres/lib/postgresql/pgxs'):
        remove(runtime / name)
    for path in (runtime / 'python').rglob('*'):
        if path.suffix == '.a' or path.name in ('__pycache__', 'ensurepip', 'idlelib', 'turtledemo'):
            remove(path)
    # PostgreSQL procedural Perl/Tcl languages are unused and pull in private
    # host Perl/Tk interfaces. SQL migrations use only pgcrypto and pg_trgm.
    for folder in (runtime / 'postgres/lib/postgresql', runtime / 'postgres/share/postgresql@16/extension'):
        if folder.is_dir():
            for path in folder.iterdir():
                if 'plperl' in path.name or path.name.startswith('pltcl'):
                    remove(path)
    contract_path = runtime / 'capabilities.json'
    contract = json.loads(contract_path.read_text())
    contract.update(distribution='app-store-lite', disabled_features=['instagram', 'linkedin', 'faces'],
                    packages={}, vcs_commits={}, vendored_distributions={}, face_models={'mode': 'unavailable'})
    contract['capabilities'] = {name: paths for name, paths in contract['capabilities'].items()
                                if name not in {'instagram', 'linkedin', 'faces'}}
    contract['capabilities']['archive_import'] = ['api/app/sync_linkedin.py', 'api/app/archive_linkedin.py',
                                                'api/app/sync_instagram_stories.py']
    contract_path.write_text(json.dumps(contract, indent=2) + '\n')
    return {'edition': 'app-store-lite', 'python_packages_kept': sorted(selected),
            'python_packages_removed': sorted(removed_packages)}

"""Run with the configured collector's environment and source path."""
from linkedin_enrichment.models import Target
from linkedin_enrichment.storage import Store


def test_crm_export_keeps_unlinked_avatar_without_changing_legacy_export(tmp_path):
    store = Store(tmp_path / 'collector.sqlite3')
    store.initialize()
    store.enqueue([
        Target(profile_url='https://www.linkedin.com/in/linked/', external_id='linked',
               metadata={'contact_id': 42, 'provider_user_id': 'linked'}),
        Target(profile_url='https://www.linkedin.com/in/unlinked/', external_id='unlinked',
               metadata={'discovered_name': 'Same name'}),
    ])
    for target, name in [(1, 'linked'), (2, 'unlinked')]:
        store.archive_snapshot(target, 'profile', {
            'name': 'Same name', 'avatar_url': f'https://media.licdn.com/{name}.jpg',
        })
    legacy = store.export_payload()['profiles']
    assert len(legacy) == 1 and legacy[0]['contact_id'] == 42
    independent = store.export_payload(include_unlinked=True)['profiles']
    assert len(independent) == 2
    unlinked = next(p for p in independent if p['provider_user_id'] == 'unlinked')
    assert unlinked['contact_id'] is None
    assert unlinked['profile']['avatar_url'] == 'https://media.licdn.com/unlinked.jpg'
    assert store.export_payload()['profiles'] == legacy

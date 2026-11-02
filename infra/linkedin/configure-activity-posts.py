"""Install the bounded SDUI post parser into the existing local collector."""
import argparse
import shutil
from pathlib import Path


def replace_once(path, old, new):
    content = path.read_text()
    if new in content:
        return
    if content.count(old) != 1:
        raise SystemExit(f'Collector layout changed: {path.name}')
    backup = path.with_name(path.name + '.before-sdui-posts')
    if not backup.exists():
        shutil.copy2(path, backup)
    temporary = path.with_name(path.name + '.sdui-tmp')
    temporary.write_text(content.replace(old, new, 1))
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    root = parser.parse_args().root
    source = root / 'src/linkedin_enrichment'
    # Validate/patch the known collector anchors before copying its new helper.
    replace_once(source / 'scrapers.py',
        '        posts = await super()._extract_posts_from_page()\n',
        '''        posts = await super()._extract_posts_from_page()
        if not posts:
            from .activity_posts import extract_activity_cards
            if not hasattr(self, "_activity_identities"):
                self._activity_identities = {}
            rows = await extract_activity_cards(self.page, identities=self._activity_identities)
            result = []
            for row in rows:
                authored, activity = classify_post_authorship(
                    self._profile_url or "", actor_profile_url=row.get("actor_profile_url"),
                    repost_header=row.get("repost_header"))
                result.append(ExtendedPost(**row, is_authored=authored, activity_type=activity))
            return result
''')
    replace_once(source / 'scrapers.py',
        '''    actor = _profile_identity(actor_profile_url)
    if target and actor:''',
        '''    actor = _profile_identity(actor_profile_url)
    if target and actor_profile_url and urlsplit(actor_profile_url).path.startswith("/company/"):
        return False, "other_actor"
    if target and actor:''')
    replace_once(source / 'scrapers.py',
        '''                if post.urn in known:
                    stop = True
                    break
''',
        '''                if post.urn in known:
                    # Pinned posts and ranked activity are not chronological.
                    # Keep scanning the bounded window for unseen posts.
                    continue
''')
    replace_once(source / 'storage.py',
                 'r"urn:li:activity:(\\d+)"',
                 'r"urn:li:(?:activity|share|ugcPost):(\\d+)"')
    shutil.copy2(Path(__file__).with_name('activity_posts.py'), source / 'activity_posts.py')
    replace_once(root / 'tests/test_scrapers.py',
        '''    assert [post.urn for post in posts] == ["new-1"]
    assert scraper.scrolls == 1''',
        '''    assert [post.urn for post in posts] == ["new-1", "older"]
    assert scraper.scrolls == 5''')
    shutil.copy2(Path(__file__).with_name('test_activity_posts.py'), root / 'tests/test_activity_posts.py')


if __name__ == '__main__':
    main()

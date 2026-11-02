"""Enable local media during the existing conservative archive schedule.

Does not restart a collector, change request delays, or clear cooldown markers.
Run on the existing collector host; original script is backed up in state/.
"""
import argparse
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root',type=Path,required=True)
root=parser.parse_args().root
p=root/'archive_profile.py';before=p.read_text()
old='''        "--media",
        action="store_true",
'''
new='''        "--media",
        action="store_true",
        default=os.environ.get("ARCHIVE_MEDIA", "0") == "1",
'''
if old in before and new not in before:
 (root/'state/archive_profile.py.before-media-20260918').write_text(before)
 p.write_text(before.replace(old,new,1))
config=root/'config.env';s=config.read_text();lines=[x for x in s.splitlines() if not x.startswith('ARCHIVE_MEDIA=')];config.write_text('\n'.join(lines+['ARCHIVE_MEDIA=1'])+'\n');config.chmod(0o600)
print('Future archive passes retain their schedule, limits and cooldowns and also download media.')

"""Publish changed files into immutable snapshots locally or over SSH.

The consumer pins destination/CURRENT once per pass. Previous snapshots remain
available for rollback and are never edited, including files shared by hardlinks.
"""
import io
import json
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

# Runs with the NAS system Python; deliberately uses only its standard library.
INSTALL = r'''
import fcntl,json,os,shutil,sys,tarfile,tempfile
from pathlib import Path
root=Path(sys.argv[1]); root.mkdir(parents=True,exist_ok=True)
with (root/'.publish.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 versions=root/'.snapshots'; versions.mkdir(exist_ok=True)
 stage=Path(tempfile.mkdtemp(prefix='release-',dir=str(versions)))
 try:
  current=root/'CURRENT'
  previous=current.resolve() if current.is_symlink() else root
  for folder,dirs,files in os.walk(str(previous),followlinks=False):
   dirs[:]=[d for d in dirs if not d.startswith('.') and d!='CURRENT' and not (Path(folder)/d).is_symlink()]
   for name in files:
    source=Path(folder)/name
    if name.startswith('.') or source.is_symlink():continue
    target=stage/source.relative_to(previous);target.parent.mkdir(parents=True,exist_ok=True)
    os.link(str(source),str(target))
  received={}; manifest=None
  with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:
   for entry in archive:
    key=Path(entry.name)
    if key.is_absolute() or '..' in key.parts or not entry.isfile():raise ValueError('Unsafe snapshot member')
    stream=archive.extractfile(entry)
    if entry.name=='.publish-manifest.json':
     manifest=json.load(stream);continue
    target=stage/key;target.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(dir=str(target.parent))
    with os.fdopen(fd,'wb') as output:
     shutil.copyfileobj(stream,output,256*1024);output.flush();os.fsync(output.fileno())
    os.replace(temp,str(target));received[entry.name]=target.stat().st_size
  if manifest is None or manifest!=received:raise ValueError('Incomplete snapshot transfer')
  with (stage/'.crm-ready').open('w') as marker:
   marker.write(stage.name);marker.flush();os.fsync(marker.fileno())
  for folder,dirs,files in os.walk(str(stage),topdown=False):
   fd=os.open(folder,os.O_RDONLY)
   try:os.fsync(fd)
   finally:os.close(fd)
  pointer=root/'.CURRENT.next'
  if pointer.is_symlink():pointer.unlink()
  pointer.symlink_to(stage.relative_to(root),target_is_directory=True)
  os.replace(str(pointer),str(current))
  fd=os.open(str(root),os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
  print(json.dumps({'snapshot':stage.name,'files':len(received)}),flush=True)
 except BaseException:
  shutil.rmtree(str(stage));raise
'''


def publish(host: str | None, destination: str, files: list[tuple[Path, str]]) -> dict:
    """Use the identical atomic installer locally or through the existing SSH transport."""
    names = set()
    for path, name in files:
        key = Path(name)
        if (path.is_symlink() or not path.is_file() or key.is_absolute() or '..' in key.parts
                or not key.parts or any(part.startswith('.') for part in key.parts) or name in names):
            raise ValueError('Unsafe or duplicate source path')
        names.add(name)
    command = shlex.join(['python3', '-c', INSTALL, destination])
    argv = ([sys.executable, '-c', INSTALL, str(Path(destination).expanduser().resolve())]
            if host is None else ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', host, command])
    process = subprocess.Popen(argv,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    sizes = {}
    try:
        with tarfile.open(fileobj=process.stdin, mode='w|') as archive:
            for path, name in files:
                if path.is_symlink() or Path(name).is_absolute() or '..' in Path(name).parts:
                    raise ValueError('Unsafe source path')
                before = path.stat()
                archive.add(path, arcname=name, recursive=False)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RuntimeError('Source changed during publication: '+name)
                sizes[name] = after.st_size
            value = json.dumps(sizes).encode()
            member = tarfile.TarInfo('.publish-manifest.json'); member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
        process.stdin.close()
        process.stdin = None
        result, _ = process.communicate(timeout=900)
        if process.returncode:
            raise RuntimeError('Snapshot publication failed; checkpoint unchanged')
        return json.loads(result)
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.stdout.close()


def publish_local(destination: Path, files: list[tuple[Path, str]]) -> dict:
    return publish(None, str(destination), files)

"""Stage an existing Monica collector archive without launching a browser."""
import argparse
import json
from pathlib import Path

MEDIA = {'.json', '.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov'}


def stage(provider, source, data_root):
    from snapshot_publish import publish_local
    source = source.resolve()
    if not source.is_dir():
        raise ValueError('请选择 Monica 已归档资料的文件夹。')
    if source == data_root.resolve() or source.is_relative_to(data_root.resolve()):
        raise ValueError('请选择导入来源文件夹，不要选择当前 Monica 数据目录。')
    if provider == 'instagram':
        if not (source / 'profiles').is_dir():
            raise ValueError('Instagram 归档需要包含 profiles 文件夹。请选择 Monica 已发布的资料归档。')
        folders = [source / 'profiles']
        destination = data_root / 'staging/instagram-current'
    elif provider == 'linkedin':
        export = source / 'linkedin_enrichment.incoming.json'
        if not export.is_file() or export.is_symlink():
            raise ValueError('LinkedIn 归档需要包含 linkedin_enrichment.incoming.json。')
        document = json.loads(export.read_text())
        if not isinstance(document, dict) or not isinstance(document.get('profiles'), list):
            raise ValueError('LinkedIn 归档格式不正确。')
        folders = [source / 'assets'] if (source / 'assets').is_dir() else []
        destination = data_root / 'raw/linkedin'
    else:
        raise ValueError('不支持此归档来源。')
    files = [(export, export.name)] if provider == 'linkedin' else []
    for folder in folders:
        if folder.is_symlink():
            raise ValueError('归档文件夹不能是符号链接。')
        for path in folder.rglob('*'):
            if path.is_symlink():
                raise ValueError('归档不能包含符号链接，请选择原始归档文件夹。')
            if path.is_file() and path.suffix.lower() in MEDIA:
                files.append((path, path.relative_to(source).as_posix()))
    if not files or (provider == 'instagram' and not any(p.suffix.lower() == '.json' for p, _ in files)):
        raise ValueError('归档中没有可导入的资料。')
    return publish_local(destination, files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('instagram', 'linkedin'), required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--data-root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.provider, args.source, args.data_root)))


if __name__ == '__main__':
    main()

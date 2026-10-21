#!/usr/bin/env python3
"""Build portable skill ZIPs from repository sources, excluding runtime state."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]/'skills'
NAMES = ('nows-ytb-dubbing',)


def package(destination):
    source = ROOT.parent/'pipeline/subtitle_layout.py'
    copied = ROOT/'nows-ytb-dubbing/scripts/subtitle_layout.py'
    if source.read_bytes() != copied.read_bytes():
        raise ValueError('Run python scripts/sync_skill_layout.py before packaging')
    destination = Path(destination).expanduser().resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError('Output directory must be outside the skill sources')
    destination.mkdir(parents=True,exist_ok=True)
    result = []
    for name in NAMES:
        source = ROOT/name
        output = destination/(name+'.zip')
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob('*')):
                relative = path.relative_to(source)
                if not path.is_file() or any(p.startswith('.') or p == '__pycache__' for p in relative.parts):
                    continue
                # Only maintained source/docs belong in a redistributable skill.
                included = (relative == Path('SKILL.md')
                            or (relative.parts[0] in ('scripts','references') and path.suffix in ('.py','.md'))
                            or (relative.parts[0] == 'assets' and path.suffix in ('.whl','.json')))
                if included:
                    member = zipfile.ZipInfo((Path(name)/relative).as_posix(), date_time=(1980,1,1,0,0,0))
                    member.compress_type = zipfile.ZIP_DEFLATED
                    member.external_attr = 0o100644 << 16
                    archive.writestr(member,path.read_bytes())
        result.append({'path':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest()})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.output),ensure_ascii=False))


if __name__ == '__main__':
    main()

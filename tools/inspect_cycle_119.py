from pathlib import Path, PurePosixPath
from zipfile import ZipFile
import json

root = Path(__file__).resolve().parents[1]
dest = root / 'artifacts/cycle-20261112T104122Z-5fe36921'
dest.mkdir(parents=True, exist_ok=True)
with ZipFile(Path.home() / 'Downloads/20261112T104122Z-5fe36921.zip') as archive:
    for item in archive.infolist():
        name = PurePosixPath(item.filename)
        if item.is_dir():
            continue
        if name.is_absolute() or len(name.parts) != 1 or '..' in name.parts or item.file_size > 10_000_000:
            raise ValueError('Unexpected archive entry')
        (dest / name.name).write_bytes(archive.read(item))
for name in ('program.json', 'recipe.json', 'activity.json', 'summary.csv', 'run.json'):
    print(name)
    print((dest / name).read_text(encoding='utf-8-sig'))
result = json.loads((dest / 'result.json').read_text())
print('result fields', list(result))
print(json.dumps({key: value for key, value in result.items()
                 if key not in ('samples','can_trace','events','activity','measurements')}, indent=2)[:18000])

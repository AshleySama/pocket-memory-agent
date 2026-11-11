# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Export the company product page to Pages, without importing private config.

Run with the website's existing Flask/Jinja environment:
    python scripts/sync_product_site.py --source-dir /path/to/PocketMemoryWebsite
Only the four public release settings and referenced static assets are exported.
"""
from pathlib import Path
import argparse
import ast
import hashlib
from html.parser import HTMLParser
import json
import re
import shutil
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

PUBLIC_KEYS = {
    'POCKET_MEMORY_DOWNLOAD_VERSION': 'download_version',
    'POCKET_MEMORY_DOWNLOAD_SIZE': 'download_size',
    'POCKET_MEMORY_DOWNLOAD_URL': 'download_url',
    'POCKET_MEMORY_DEMO_VIDEO_URL': 'demo_video_url',
}


class ResourceLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        for key in ('href', 'src', 'poster'):
            if attrs.get(key):
                self.paths.append(attrs[key])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', required=True, type=Path)
    args = parser.parse_args()
    source = args.source_dir.resolve(strict=True)
    target = Path(__file__).resolve().parents[1] / 'site'
    settings = {}
    for node in ast.parse((source / 'config.py').read_text(encoding='utf-8-sig')).body:
        if isinstance(node, ast.Assign):
            for name in node.targets:
                if isinstance(name, ast.Name) and name.id in PUBLIC_KEYS:
                    settings[PUBLIC_KEYS[name.id]] = ast.literal_eval(node.value)
    if set(settings) != set(PUBLIC_KEYS.values()):
        raise ValueError('Missing public release settings')
    for key in ('download_url', 'demo_video_url'):
        url = urlsplit(settings[key])
        if url.scheme != 'https' or url.query or url.fragment:
            raise ValueError('Use permanent HTTPS media URLs without signed queries')

    def static_url(endpoint, filename):
        if endpoint != 'static' or Path(filename).is_absolute() or '..' in Path(filename).parts:
            raise ValueError('Only relative public static assets can be exported')
        return 'static/' + filename

    environment = Environment(loader=FileSystemLoader(source / 'templates'),
                              autoescape=select_autoescape(['html']),
                              undefined=StrictUndefined)
    rendered = environment.get_template('index.html').render(url_for=static_url, **settings)
    notice = '<!-- Copyright 2026 AshleySama | SPDX-License-Identifier: Apache-2.0 -->'
    rendered = rendered.replace('<!doctype html>', '<!doctype html>\n' + notice + '\n', 1)
    page = ResourceLinks()
    page.feed(rendered)
    queue = [(link, source) for link in page.paths]
    copied = {}
    static_root = (source / 'static').resolve(strict=True)
    while queue:
        link, parent = queue.pop()
        url = urlsplit(link)
        if url.scheme or url.netloc or not url.path:
            continue
        path = (parent / unquote(url.path)).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(static_root):
            raise ValueError('Resource is outside public static assets: ' + url.path)
        relative = path.relative_to(source)
        key = relative.as_posix()
        if key in copied:
            continue
        data = path.read_bytes()
        copied[key] = hashlib.sha256(data).hexdigest()
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        if path.suffix == '.css':
            queue.extend((value, path.parent) for value in re.findall(r'url\([\"\']?([^\)\"\']+)', data.decode('utf-8')))
        elif path.suffix == '.html':
            child = ResourceLinks()
            child.feed(data.decode('utf-8'))
            queue.extend((value, path.parent) for value in child.paths)

    target.mkdir(parents=True, exist_ok=True)
    (target / 'index.html').write_text(rendered + '\n', encoding='utf-8', newline='\n')
    result = {
        'source': 'PocketMemoryWebsite/templates/index.html',
        'template_sha256': hashlib.sha256((source / 'templates/index.html').read_bytes()).hexdigest(),
        'version': settings['download_version'],
        'download_url': settings['download_url'],
        'video_url': settings['demo_video_url'],
        'assets': copied,
        'index_sha256': hashlib.sha256((target / 'index.html').read_bytes()).hexdigest(),
    }
    (target / 'site-sync.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'version': result['version'], 'copied_assets': len(copied), 'index_sha256': result['index_sha256']}, ensure_ascii=False))


if __name__ == '__main__':
    main()

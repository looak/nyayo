"""Fetches an official fastchess release's files, with their sha256, for the workers: manage.py fetch_fastchess <version>."""

import json
import os
import urllib.error
import urllib.request

from django.core.management.base import BaseCommand, CommandError

from nyayo.lab import fastchess


def get(url, timeout=60):
    request = urllib.request.Request(url, headers={'User-Agent': 'nyayo', 'Accept': 'application/vnd.github+json'})
    return urllib.request.urlopen(request, timeout=timeout)


class Command(BaseCommand):
    help = ("Downloads the files of an official fastchess release (Disservin/fastchess) into DATA_DIR/fastchess/<version>/, "
            "with their sha256 in a manifest. clientMatchRunnerVersionRef then offers the file for a worker's system.")

    def add_arguments(self, parser):
        parser.add_argument('version', help='the release, by its tag: v1.8.1-alpha, or 1.8.1-alpha')
        parser.add_argument('--api', default=fastchess.RELEASES_API, help='the releases API to ask (default: GitHub\'s for Disservin/fastchess)')

    def handle(self, *args, version, api, **options):
        data = None
        for tag in dict.fromkeys([version, 'v' + version.lstrip('v')]):
            try:
                with get('%s/releases/tags/%s' % (api.rstrip('/'), tag)) as response:
                    data, found = json.loads(response.read().decode('utf-8')), tag
                break
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise CommandError('The releases API answered %d' % error.code)
            except OSError as error:
                raise CommandError('Could not reach the releases API: %s' % error)
        if data is None:
            raise CommandError('There is no release %s' % version)

        target = fastchess.directory() / found
        target.mkdir(parents=True, exist_ok=True)
        files = []
        for asset in data.get('assets') or []:
            name = asset.get('name', '')
            if not fastchess.FILE.match(name) or not asset.get('browser_download_url'):
                self.stderr.write('Skipped %r: not a name nyayo serves' % name)
                continue
            partial = target / (name + '.part')
            try:
                with get(asset['browser_download_url'], timeout=300) as response, open(partial, 'wb') as out:
                    for chunk in iter(lambda: response.read(1 << 20), b''):
                        out.write(chunk)
            except OSError as error:
                partial.unlink(missing_ok=True)
                raise CommandError('Could not download %s: %s' % (name, error))
            sha = fastchess.sha256_of(partial)
            digest = str(asset.get('digest') or '')
            if digest.startswith('sha256:') and digest[7:].lower() != sha:
                partial.unlink(missing_ok=True)
                raise CommandError('%s does not match the sha256 GitHub gives for it' % name)
            os.replace(partial, target / name)
            system, arch = fastchess.classify(name)
            files.append({'name': name, 'sha256': sha, 'os': system, 'arch': arch, 'size': (target / name).stat().st_size})
            self.stdout.write('%s  %s  %s %s' % (sha, name, system or '-', arch or '-'))
        if not files:
            raise CommandError('Release %s has no files' % found)
        (target / 'manifest.json').write_text(json.dumps({'version': found, 'files': files}, indent=1), encoding='utf-8')
        self.stdout.write('Fetched %d files of %s into %s' % (len(files), found, target))

"""One disposable local Docker container per evaluation; no host shell tools."""
import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

from .data import fingerprint, sha256
from .evidence import regular_file


def tree_hash(root):
    """Reject links and special files instead of importing host-side references."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f'Expected a regular directory: {root}')
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f'Unsupported input entry: {path}')
        if path.is_file():
            files[path.relative_to(root).as_posix()] = sha256(path)
    return fingerprint(files)


class DockerSandbox:
    def __init__(self, workspace, evaluation, *, image, network='none'):
        self.workspace = Path(workspace).resolve()
        self.evaluation = Path(evaluation).resolve()
        self.image = image
        self.network = network
        self.name = 'vas-judge-' + uuid4().hex

    def __enter__(self):
        if not shutil.which('docker'):
            raise RuntimeError('Docker is required. Install/start Docker and build sandbox/Dockerfile.')
        if self.network not in {'none', 'bridge'}:
            raise ValueError('Sandbox network must be none or bridge')
        if any(',' in str(p) for p in (self.workspace, self.evaluation)):
            raise ValueError('Docker bind paths must not contain commas')
        args = [
            'docker', 'run', '-d', '--name', self.name, '--init', '--read-only',
            '--network', self.network, '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--pids-limit', '512',
            '--cpus', '2', '--memory', '4g', '--shm-size', '512m',
            '--tmpfs', '/tmp:rw,size=1g,mode=1777',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '--env', 'HOME=/tmp', '--env', 'PYTHONDONTWRITEBYTECODE=1',
            '--env', 'PLAYWRIGHT_BROWSERS_PATH=/opt/browsers',
            '--mount', f'type=bind,source={self.workspace},target=/work,readonly',
            '--mount', f'type=bind,source={self.evaluation},target=/work/evaluation',
            '--workdir', '/work', self.image, 'sleep', 'infinity',
        ]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError('Docker sandbox could not start: ' + result.stderr[-2000:])
            self.image_id = subprocess.check_output(
                ['docker', 'inspect', '--format', '{{.Image}}', self.name],
                text=True, timeout=15).strip()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        subprocess.run(['docker', 'rm', '-f', self.name], capture_output=True, timeout=30)

    def shell(self, command, timeout):
        if not isinstance(command, str) or not command.strip():
            raise ValueError('command must be a nonempty string')
        if type(timeout) is not int or timeout < 1:
            raise ValueError('timeout must be a positive integer')
        args = ['docker', 'exec', self.name, 'timeout', '--signal=KILL',
                str(timeout), 'bash', '-lc', command]
        # Bound memory even when a command prints a large file.
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT,
                                         timeout=timeout + 10)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError('Docker exec stalled; evaluation aborted') from error
            size = output.tell()
            output.seek(0)
            text = output.read(32000).decode('utf-8', errors='replace')
        return {'exit_code': process.returncode, 'output': text,
                'truncated': size > 32000}


def image_output(case, name):
    """Expose only retained evidence images, never arbitrary host files."""
    path = Path(name)
    if len(path.parts) < 3 or path.parts[:2] not in {
        ('evaluation', 'evidence'), ('evaluation', 'screenshots')
    }:
        raise ValueError('Images must be retained under evaluation/evidence/ or screenshots/')
    image = regular_file(Path(case), name)
    mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.webp': 'image/webp'}.get(image.suffix.lower())
    if not mime or image.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Use a PNG/JPEG/WebP image of at most 8 MiB')
    encoded = base64.b64encode(image.read_bytes()).decode('ascii')
    return [{'type': 'input_text', 'text': 'Retained image: ' + name},
            {'type': 'input_image', 'image_url': f'data:{mime};base64,{encoded}',
             'detail': 'high'}]

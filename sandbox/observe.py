"""Render a browser view, PDF pages, or a video frame into retained evidence."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    tools = parser.add_subparsers(dest='kind', required=True)
    web = tools.add_parser('web')
    web.add_argument('url')
    web.add_argument('output', type=Path)
    web.add_argument('--width', type=int, default=1440)
    web.add_argument('--height', type=int, default=900)
    web.add_argument('--wait-ms', type=int, default=1500)
    pdf = tools.add_parser('pdf')
    pdf.add_argument('path', type=Path)
    pdf.add_argument('output_prefix', type=Path)
    pdf.add_argument('--first', type=int, default=1)
    pdf.add_argument('--last', type=int, default=1)
    video = tools.add_parser('frame')
    video.add_argument('path', type=Path)
    video.add_argument('output', type=Path)
    video.add_argument('--time', default='00:00:01')
    args = parser.parse_args()
    if args.kind == 'web':
        from playwright.sync_api import sync_playwright
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': args.width, 'height': args.height})
            page.goto(args.url, wait_until='load', timeout=45000)
            page.wait_for_timeout(args.wait_ms)
            page.screenshot(path=str(args.output), full_page=True)
            print({'title': page.title(), 'url': page.url, 'screenshot': str(args.output)})
            browser.close()
    elif args.kind == 'pdf':
        args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['pdftoppm', '-png', '-r', '110', '-f', str(args.first),
                        '-l', str(args.last), str(args.path), str(args.output_prefix)], check=True)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-ss', args.time, '-i', str(args.path),
                        '-frames:v', '1', '-y', str(args.output)], check=True)


if __name__ == '__main__':
    main()

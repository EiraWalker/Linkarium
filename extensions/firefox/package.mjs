import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';
const root = fileURLToPath(new URL('../..', import.meta.url));
const python = resolve(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
execFileSync(python, ['-X', 'utf8', resolve(root, 'scripts/build_firefox_extension.py'), ...process.argv.slice(2)], {stdio: 'inherit'});

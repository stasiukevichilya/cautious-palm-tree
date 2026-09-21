"""Create an anonymous Docker config, preserving context and CLI plugin discovery.

Only for public image pulls/builds. Never copies credentials or invokes helpers.
"""
import json
import sys
from pathlib import Path

source = Path(sys.argv[1]).expanduser().resolve()
target = Path(sys.argv[2]).resolve()
original = source / 'config.json'
config = json.loads(original.read_text(encoding='utf-8')) if original.exists() else {}
public = {'auths': {}, 'credsStore': '', 'credHelpers': {}}
if config.get('currentContext'):
    public['currentContext'] = config['currentContext']
if config.get('cliPluginsExtraDirs'):
    public['cliPluginsExtraDirs'] = config['cliPluginsExtraDirs']
for name in ('contexts', 'cli-plugins'):
    path = source / name
    if path.exists():
        (target / name).symlink_to(path, target_is_directory=True)
(target / 'config.json').write_text(json.dumps(public) + '\n', encoding='utf-8')

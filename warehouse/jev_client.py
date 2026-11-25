"""Minimal stdio client for the Jev MCP server (TypeSafe judgments), for batch use from warehouse scripts.

The server is started exactly as the Claude Code MCP config starts it (`npx -y @jkudish/jev-mcp` after
sourcing ~/.zshenv), with JEV_PROVIDER / JEV_MCP_MODEL taken from that config. No key is read or printed
here; the server inherits it from the shell environment.

    with Jev() as j:
        out = j.call('jev_classify', {'items': [...], 'classes': [...]})
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _config_env() -> dict[str, str]:
    cfg = json.loads((Path.home() / '.claude.json').read_text())
    env = cfg.get('mcpServers', {}).get('jev', {}).get('env', {}) or {}
    return {k: v for k, v in env.items() if k in ('JEV_PROVIDER', 'JEV_MCP_MODEL')}


class Jev:
    def __init__(self, timeout: float = 600) -> None:
        self.timeout = timeout
        self.next_id = 0

    def __enter__(self) -> 'Jev':
        env = {**os.environ, **_config_env()}
        self.p = subprocess.Popen(['zsh', '-c', 'source ~/.zshenv >/dev/null 2>&1; exec npx -y @jkudish/jev-mcp'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self._rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'nba-warehouse', 'version': '1'}})
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return self

    def __exit__(self, *exc) -> None:
        self.p.stdin.close()
        self.p.terminate()

    def _send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg) + '\n')
        self.p.stdin.flush()

    def _rpc(self, method: str, params: dict) -> dict:
        self.next_id += 1
        mid = self.next_id
        self._send({'jsonrpc': '2.0', 'id': mid, 'method': method, 'params': params})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError('jev-mcp exited')
            msg = json.loads(line)
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise RuntimeError(f"jev error: {msg['error']}")
                return msg['result']

    def call(self, tool: str, args: dict) -> dict:
        r = self._rpc('tools/call', {'name': tool, 'arguments': args})
        if r.get('isError'):
            raise RuntimeError(f'{tool} failed: ' + ' '.join(c.get('text', '') for c in r.get('content', []))[:2000])
        if 'structuredContent' in r:
            return r['structuredContent']
        return json.loads(''.join(c.get('text', '') for c in r.get('content', []) if c.get('type') == 'text'))

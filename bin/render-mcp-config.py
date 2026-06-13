#!/usr/bin/env python3
"""Render an MCP config template by expanding ${VAR} / ${VAR:-default} from the environment.

Claude Code's `--mcp-config` does not reliably expand environment variables (it varies by version),
so the worker renders the template to a concrete config just before launching claude. The MCP env
values live in the worker's login-shell profile, so this MUST run in that environment (the dispatcher
does so via the worker script).

Correctly handles nested braces inside defaults — e.g. `${PROD_RDS_HOST_PATTERN:-{svc}.example.com}` —
which a naive `[^}]*` regex mangles. Reads the template path from argv[1] (or $AGENT_MCP_TEMPLATE)
and writes the resolved JSON to stdout. Exits non-zero if the result isn't valid JSON.
"""
import json
import os
import sys


def expand(s):
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == "$" and i + 1 < n and s[i + 1] == "{":
            depth, j = 1, i + 2
            while j < n and depth > 0:
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = s[i + 2:j]                       # VAR  or  VAR:-default
            var, sep, default = inner.partition(":-")
            if var in os.environ:
                out.append(os.environ[var])
            else:
                out.append(expand(default) if sep else "")
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ["AGENT_MCP_TEMPLATE"]
    rendered = expand(open(path).read())
    json.loads(rendered)  # fail loudly if expansion produced invalid JSON
    sys.stdout.write(rendered)


if __name__ == "__main__":
    main()

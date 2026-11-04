"""Checks that every plugin only uses what DCS Core provides, before anything is published.

For each plugin folder in ./plugins (other than dcs_core) it checks:
  - the DCS Core API version the plugin asks for (CORE_API / DCS_API = N) is not newer than DCS Core's API
  - every dcs_core module, function, class and constant it uses exists in plugins/dcs_core
  - every keyword argument it passes to a DCS Core function or class is accepted

Run:   python check_dcs_api.py            (exit code 1 when there is a problem)
       python check_dcs_api.py --list     (also lists what each plugin uses)
Needs only standard Python. Tests folders are skipped.
"""
import ast
import collections
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.join(ROOT, "plugins")
SKIP_DIRS = {"__pycache__", "tests", "test", "ext", ".git"}


def _py_files(folder):
    for dp, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dp, f)


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), path)


def core_definitions(core):
    """{module: {name: set of parameter names, or None for constants/unknown}} and the API number."""
    defs, api = {}, None
    for f in sorted(os.listdir(core)):
        if not f.endswith(".py"):
            continue
        mod = "dcs_core" if f == "__init__.py" else "dcs_core." + f[:-3]
        d = {}
        for n in _parse(os.path.join(core, f)).body:
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                fn = n if isinstance(n, ast.FunctionDef) else next(
                    (x for x in n.body if isinstance(x, ast.FunctionDef) and x.name == "__init__"), None)
                if fn is None:
                    d[n.name] = None
                else:
                    a = fn.args
                    d[n.name] = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs} | ({"**"} if a.kwarg else set())
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            d[x.id] = None
                            if mod == "dcs_core" and x.id == "API" and isinstance(n.value, ast.Constant):
                                api = n.value.value
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    d[(a.asname or a.name).split(".")[0]] = None
        defs[mod] = d
    return defs, api


def plugin_uses(folder, defs):
    """{(module, name, keyword or None): set of 'file:line'} and the API numbers the plugin asks for."""
    use = collections.defaultdict(set)
    apis = set()
    for p in _py_files(folder):
        t = _parse(p)
        rel = os.path.relpath(p, PLUGINS)
        alias = {}          # local name -> module (str) or (module, name)
        for n in ast.walk(t):
            if isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] == "dcs_core":
                for a in n.names:
                    sub = n.module + "." + a.name
                    if n.module == "dcs_core" and sub in defs:
                        alias[a.asname or a.name] = sub
                    else:
                        use[(n.module, a.name, None)].add("%s:%d" % (rel, n.lineno))
                        alias[a.asname or a.name] = (n.module, a.name)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.split(".")[0] == "dcs_core":
                        alias[a.asname or a.name] = a.name
            elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, int):
                if any(isinstance(x, ast.Name) and x.id in ("CORE_API", "DCS_API") for x in n.targets):
                    apis.add(n.value.value)
        for n in ast.walk(t):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and isinstance(alias.get(n.value.id), str):
                use[(alias[n.value.id], n.attr, None)].add("%s:%d" % (rel, n.lineno))
            if isinstance(n, ast.Call):
                target = None
                if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) \
                        and isinstance(alias.get(n.func.value.id), str):
                    target = (alias[n.func.value.id], n.func.attr)
                elif isinstance(n.func, ast.Name) and isinstance(alias.get(n.func.id), tuple):
                    target = alias[n.func.id]
                if target:
                    for k in n.keywords:
                        if k.arg:
                            use[(target[0], target[1], k.arg)].add("%s:%d" % (rel, n.lineno))
    return use, apis


def main(argv):
    core = os.path.join(PLUGINS, "dcs_core")
    if not os.path.isdir(core):
        print("check_dcs_api: no plugins/dcs_core folder")
        return 1
    defs, api = core_definitions(core)
    problems = 0
    for name in sorted(os.listdir(PLUGINS)):
        folder = os.path.join(PLUGINS, name)
        if name == "dcs_core" or not os.path.isfile(os.path.join(folder, "metadata.txt")):
            continue
        use, apis = plugin_uses(folder, defs)
        if not use:
            continue
        print("%s: uses %d DCS Core names, asks for API %s (DCS Core has API %s)"
              % (name, len({(m, n) for m, n, _k in use}), "/".join(map(str, sorted(apis))) or "-", api))
        for need in apis:
            if api is not None and need > api:
                print("  PROBLEM  needs DCS Core API %d but plugins/dcs_core has API %d" % (need, api))
                problems += 1
        missing_mods = set()
        for (mod, attr, kw), where in sorted(use.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")):
            at = ", ".join(sorted(where)[:3]) + (" ..." if len(where) > 3 else "")
            d = defs.get(mod)
            if d is None:
                if mod in missing_mods:
                    continue
                missing_mods.add(mod)
                msg = "module %s does not exist" % mod
            elif attr not in d:
                msg = "%s.%s does not exist" % (mod, attr)
            elif kw and d[attr] is not None and kw not in d[attr] and "**" not in d[attr]:
                msg = "%s.%s does not take %s=" % (mod, attr, kw)
            else:
                if "--list" in argv:
                    print("  ok       %s.%s%s" % (mod, attr, "(%s=)" % kw if kw else ""))
                continue
            print("  PROBLEM  %s  (%s)" % (msg, at))
            problems += 1
    print("check_dcs_api: %s" % ("OK" if not problems else "%d problem(s)" % problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

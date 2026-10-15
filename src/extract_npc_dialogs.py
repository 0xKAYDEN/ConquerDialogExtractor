#!/usr/bin/env python3
"""
Extract NPC dialogs from a C# NpcHandler class into a Lua string table.

  python extract_npc_dialogs.py --npc-id NpcID.cs --handler NpcHandler.cs --out-dir npc_strings

For every method marked with  [NpcAttribute(NpcID.Something)]  it finds all
  data.AddText(...)   -> STR_ID_<NpcName>[<NpcId>][Text<n>]
  data.AddOption(...) -> STR_ID_<NpcName>[<NpcId>][Option<n>]
<NpcName> comes from the NpcID enum (matched by the enum member name).

Output (in --out-dir):
  common.lua                 strings repeated at least --min-dup times (default 3),
                             each with a numeric id: STR_COMMON[1], STR_COMMON[2]...
  npcs/<Name>-<Id>.lua       one file per NPC, NPCs sorted by id; entries that use a
                             common string store a reference ("@STR_COMMON[n]").

Dynamic parts of a string (variables, property access, ...) become {0}, {1}...
and the original C# expressions are written in a comment above the entry.
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

# --------------------------------------------------------------------------
# Low level C# scanning helpers (string / comment aware)
# --------------------------------------------------------------------------


def string_end(src, i):
    """If a C# string literal starts at i (with optional $ / @ prefixes) return the index after it."""
    j = i
    verbatim = False
    while j < len(src) and src[j] in "@$":
        if src[j] == "@":
            verbatim = True
        j += 1
    if j >= len(src) or src[j] != '"':
        return None
    j += 1
    while j < len(src):
        c = src[j]
        if verbatim:
            if c == '"':
                if src[j + 1:j + 2] == '"':
                    j += 2
                    continue
                return j + 1
        else:
            if c == "\\":
                j += 2
                continue
            if c == '"':
                return j + 1
        j += 1
    return len(src)


def char_end(src, i):
    j = i + 1
    while j < len(src):
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == "'":
            return j + 1
        j += 1
    return len(src)


def skip_literal(src, i):
    """Return index after a string/char literal starting at i, or None."""
    c = src[i]
    if c in '"@$':
        return string_end(src, i)
    if c == "'":
        return char_end(src, i)
    return None


def strip_comments(src):
    """Blank out // and /* */ comments (keeps newlines so offsets stay stable)."""
    out = []
    i, n = 0, len(src)
    while i < n:
        end = skip_literal(src, i)
        if end:
            out.append(src[i:end])
            i = end
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(re.sub(r"[^\n]", " ", src[i:j]))
            i = j
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def match_close(src, open_idx, open_ch, close_ch):
    depth = 0
    i = open_idx
    while i < len(src):
        end = skip_literal(src, i)
        if end:
            i = end
            continue
        c = src[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(src) - 1


def split_top(expr, sep):
    """Split on `sep` at nesting depth 0, ignoring separators inside strings/brackets."""
    parts, cur, depth, i = [], [], 0, 0
    while i < len(expr):
        end = skip_literal(expr, i)
        if end:
            cur.append(expr[i:end])
            i = end
            continue
        c = expr[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


# --------------------------------------------------------------------------
# C# expression -> (template, args)
# --------------------------------------------------------------------------

SIMPLE_ESC = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"',
              "'": "'", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def unescape(s):
    def rep(m):
        e = m.group(1)
        if e[0] in "uU":
            return chr(int(e[1:], 16))
        if e[0] == "x":
            return chr(int(e[1:], 16))
        return SIMPLE_ESC.get(e, e)
    return re.sub(r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{1,4}|.)", rep, s, flags=re.S)


class Template:
    def __init__(self):
        self.text = []
        self.args = []

    def lit(self, s):
        self.text.append(s)

    def arg(self, expr):
        expr = " ".join(expr.split())
        if expr not in self.args:
            self.args.append(expr)
        self.text.append("{%d}" % self.args.index(expr))


def add_token(tpl, tok):
    tok = tok.strip()
    m = re.match(r'^([@$]*)"', tok)
    if not m or not tok.endswith('"') or len(tok) < len(m.group(0)) + 1:
        tpl.arg(tok)
        return
    prefix = m.group(1)
    body = tok[len(prefix) + 1:-1]
    verbatim, interp = "@" in prefix, "$" in prefix

    def clean(chunk):
        return chunk.replace('""', '"') if verbatim else unescape(chunk)

    if not interp:
        tpl.lit(clean(body))
        return
    i, buf = 0, []
    while i < len(body):
        c = body[i]
        if body.startswith("{{", i):
            buf.append("{")
            i += 2
        elif body.startswith("}}", i):
            buf.append("}")
            i += 2
        elif c == "{":
            j = match_close(body, i, "{", "}")
            expr = body[i + 1:j]
            expr = re.split(r",\s*-?\d+\s*$|:(?![^(]*\))", expr)[0]
            tpl.lit(clean("".join(buf)))
            buf = []
            tpl.arg(expr)
            i = j + 1
        else:
            buf.append(c)
            i += 1
    tpl.lit(clean("".join(buf)))


def parse_expr(expr):
    tpl = Template()
    for tok in split_top(expr, "+"):
        if tok.strip():
            add_token(tpl, tok)
    return "".join(tpl.text), tpl.args


# --------------------------------------------------------------------------
# NpcID enum + handler parsing
# --------------------------------------------------------------------------


def read_all(paths):
    files = []
    for p in paths:
        p = Path(p)
        files += sorted(p.rglob("*.cs")) if p.is_dir() else [p]
    return [(f, f.read_text(encoding="utf-8-sig", errors="replace")) for f in files]


def parse_npc_ids(paths):
    name_to_id, id_to_name = {}, {}
    for _, src in read_all(paths):
        src = strip_comments(src)
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*(0[xX][0-9a-fA-F]+|\d+)\s*,?", src):
            v = int(m.group(2), 0)
            name_to_id.setdefault(m.group(1), v)
            id_to_name.setdefault(v, m.group(1))
    return name_to_id, id_to_name


def parse_handlers(paths, attr, name_to_id, id_to_name):
    attr_re = re.compile(
        r"\[\s*" + re.escape(attr) + r"\s*\(\s*(?:\(\s*\w+\s*\)\s*)?"
        r"(?:\w+\.(\w+)|(0[xX][0-9a-fA-F]+|\d+))\s*\)\s*\]")
    npcs = []
    for f, src in read_all(paths):
        src = strip_comments(src)
        matches = list(attr_re.finditer(src))
        k = 0
        while k < len(matches):
            group = [matches[k]]
            while k + 1 < len(matches) and src[matches[k].end():matches[k + 1].start()].strip() == "":
                k += 1
                group.append(matches[k])
            k += 1
            ids = []
            for m in group:
                if m.group(1):
                    if m.group(1) in name_to_id:
                        ids.append((m.group(1), name_to_id[m.group(1)]))
                    else:
                        print(f"[warn] {f.name}: '{m.group(1)}' not found in NpcID class", file=sys.stderr)
                else:
                    v = int(m.group(2), 0)
                    ids.append((id_to_name.get(v, f"NPC{v}"), v))
            if not ids:
                continue
            start = group[-1].end()
            brace = src.find("{", start)
            semi = src.find(";", start)
            if brace == -1 or (semi != -1 and semi < brace):
                continue
            body = src[brace + 1:match_close(src, brace, "{", "}")]
            npcs.append({"ids": ids, "body": body})
    return npcs


CALL_RE = re.compile(r"\b[\w.]+\s*\.\s*(AddText|AddOption)\s*\(")


def extract_dialog(body):
    texts, options = [], []
    for m in CALL_RE.finditer(body):
        open_idx = m.end() - 1
        inner = body[open_idx + 1:match_close(body, open_idx, "(", ")")]
        args = split_top(inner, ",")
        if not args or not args[0].strip():
            continue
        template, targs = parse_expr(args[0])
        if m.group(1) == "AddText":
            texts.append((template, targs))
        else:
            opt_id = args[1].strip() if len(args) > 1 else None
            options.append((template, targs, opt_id))
    return texts, options


# --------------------------------------------------------------------------
# Lua output
# --------------------------------------------------------------------------


def lua_str(s):
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32:
            out.append("\\%03d" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def norm_key(kind, tpl, strict):
    """Key used to decide that two strings 'mean the same thing'."""
    if strict:
        return (kind, tpl)
    nl = len(tpl) - len(tpl.rstrip("\n"))
    core = " ".join(tpl.rstrip("\n").lower().split())
    # '~' is used as a space in dialog options; every other symbol is ignored,
    # only letters, digits and {n} placeholders are compared.
    words = "".join(ch if (ch.isalnum() or ch in "{}") else " " for ch in core.replace("~", " "))
    words = " ".join(words.split())
    return (kind, words or core, nl)  # symbol-only strings ("-----", "...") compare as written


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--npc-id", nargs="+", required=True, help="file(s)/dir(s) containing the NpcID class")
    ap.add_argument("--handler", nargs="+", required=True, help="file(s)/dir(s) containing the NpcHandler class")
    ap.add_argument("--out-dir", default="npc_strings", help="output folder (gets common.lua and npcs/)")
    ap.add_argument("--attr", default="NpcAttribute", help="attribute name used on handler methods")
    ap.add_argument("--min-dup", type=int, default=3,
                    help="move a string to common.lua when it appears at least this many times")
    ap.add_argument("--strict-dup", action="store_true",
                    help="only merge identical strings (default also merges case/punctuation/~ variants)")
    ap.add_argument("--report", help="write a file listing which spellings were merged into each COMMON id")
    ap.add_argument("--common-start", type=int, default=1, help="first id used for COMMON strings")
    ap.add_argument("--ref-prefix", default="@", help="prefix marking a value as a reference to a COMMON key")
    ap.add_argument("--option-by-id", action="store_true",
                    help="use the AddOption id (1, 2, 255...) as the option index instead of 1..n")
    ap.add_argument("--strip-newlines", action="store_true", help="remove trailing \\n from texts")
    ap.add_argument("--no-table", action="store_true", help="don't wrap each file in 'return { ... }'")
    a = ap.parse_args()

    name_to_id, id_to_name = parse_npc_ids(a.npc_id)
    npcs = parse_handlers(a.handler, a.attr, name_to_id, id_to_name)

    # ---- one record per NPC id (several handler methods for one id are merged) ----
    merged = {}
    for npc in npcs:
        texts, options = extract_dialog(npc["body"])
        if not texts and not options:
            continue
        name, npc_id = npc["ids"][0]
        m = merged.setdefault(npc_id, dict(name=name, id=npc_id, aliases=[], texts=[], options=[]))
        for n, i in npc["ids"][1:]:
            if f"{n}={i}" not in m["aliases"]:
                m["aliases"].append(f"{n}={i}")
        m["texts"] += texts
        m["options"] += options

    # ---- build entries, NPCs sorted by id ----------------------------------
    entries = []
    for npc_id in sorted(merged):
        m = merged[npc_id]
        base = dict(npc=m["name"], id=npc_id, aliases=m["aliases"])
        for n, (tpl, args) in enumerate(m["texts"], 1):
            if a.strip_newlines:
                tpl = tpl.rstrip("\n")
            entries.append(dict(base, kind="Text", idx=str(n), tpl=tpl, args=args))
        for n, (tpl, args, oid) in enumerate(m["options"], 1):
            idx = oid if (a.option_by_id and oid) else str(n)
            entries.append(dict(base, kind="Option", idx=idx, tpl=tpl, args=args))

    # ---- find duplicated strings -------------------------------------------
    for e in entries:
        e["nk"] = norm_key(e["kind"], e["tpl"], a.strict_dup)
    counts = Counter(e["nk"] for e in entries if e["tpl"].strip())
    variants = {}  # nk -> Counter of the exact texts seen
    for e in entries:
        variants.setdefault(e["nk"], Counter())[e["tpl"]] += 1
    common = {}  # nk -> (key, canonical text), numbered by first appearance (NPCs sorted by id)
    for e in entries:
        nk = e["nk"]
        if counts.get(nk, 0) >= a.min_dup and nk not in common:
            text = max(variants[nk].items(), key=lambda kv: kv[1])[0]  # most frequent spelling
            common[nk] = (f"STR_COMMON[{a.common_start + len(common)}]", text)

    out_dir = Path(a.out_dir)
    npc_dir = out_dir / "npcs"
    npc_dir.mkdir(parents=True, exist_ok=True)
    ind = "" if a.no_table else "    "

    def write_lua(path, body):
        lines = body if a.no_table else ["return {"] + body + ["}"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- common.lua --------------------------------------------------------
    body = [f"{ind}-- strings used {a.min_dup}+ times; NPC entries refer to them as \"{a.ref_prefix}<key>\""]
    for key, text in common.values():
        body.append(f"{ind}[{lua_str(key)}] = {lua_str(text)},")
    write_lua(out_dir / "common.lua", body)

    # ---- npcs/<Name>-<Id>.lua ----------------------------------------------
    files = 0
    for npc_id in sorted(merged):
        m = merged[npc_id]
        alias = f"  (also: {', '.join(m['aliases'])})" if m["aliases"] else ""
        body = [f"{ind}-- {m['name']} [{npc_id}]{alias}"]
        for e in (x for x in entries if x["id"] == npc_id):
            key = f"STR_ID_{e['npc']}[{e['id']}][{e['kind']}{e['idx']}]"
            ref = common[e["nk"]][0] if e["nk"] in common else None
            value = lua_str(a.ref_prefix + ref) if ref else lua_str(e["tpl"])
            if e["args"]:
                body.append(f"{ind}-- " + ", ".join(f"{{{i}}}={x}" for i, x in enumerate(e["args"])))
            body.append(f"{ind}[{lua_str(key)}] = {value},")
        write_lua(npc_dir / f"{m['name']}-{npc_id}.lua", body)
        files += 1

    if a.report:
        rep = []
        for nk, (key, text) in common.items():
            if len(variants[nk]) > 1:
                rep.append(f"{key}  kept: {text!r}")
                for v, c in variants[nk].most_common():
                    rep.append(f"    {c:>4}x  {v!r}")
        Path(a.report).write_text("\n".join(rep) + "\n", encoding="utf-8")

    print(f"{files} NPC files, {len(entries)} strings, {len(common)} in common.lua -> {out_dir}/")


if __name__ == "__main__":
    main()
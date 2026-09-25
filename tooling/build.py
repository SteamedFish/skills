#!/usr/bin/env python3
"""skills catalog build tool.

Subcommands:
  sync      fetch third-party sources, rebuild vendor/, write lockfile/report
  validate  check all catalog references resolve and frontmatter parses
  build     validate, then rebuild dist/ with rewritten frontmatter + index.json
"""

import argparse
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_YAML = os.path.join(ROOT, "sources.yaml")
CATALOGS_YAML = os.path.join(ROOT, "catalogs.yaml")
LOCK_FILE = os.path.join(ROOT, "sources.lock.json")
REPORT_FILE = os.path.join(ROOT, "sources.report.json")
SKILLS_DIR = os.path.join(ROOT, "skills")
VENDOR_DIR = os.path.join(ROOT, "vendor")
DIST_DIR = os.path.join(ROOT, "dist")

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
LICENSE_PATTERNS = ("LICENSE", "LICENCE", "COPYING")


def log(msg):
    print(msg)


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_sources():
    data = load_yaml(SOURCES_YAML) or {}
    return data.get("sources") or []


def load_catalogs():
    data = load_yaml(CATALOGS_YAML) or {}
    return data.get("catalogs") or {}


def iter_catalogs(catalogs):
    """Yield (name, catalog_dict) for global + each project catalog."""
    for key, cat in catalogs.items():
        if key == "projects":
            continue
        yield key, cat
    for name, cat in (catalogs.get("projects") or {}).items():
        yield name, cat


def parse_ref(skill_id):
    """'name' -> (None, 'name'); 'source/name' -> (source, name)."""
    parts = skill_id.split("/")
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    fail(f"invalid skill id: {skill_id}")


def resolve_skill_dir(source, name):
    if source is None:
        return os.path.join(SKILLS_DIR, name)
    return os.path.join(VENDOR_DIR, source, name)


def parse_frontmatter(path):
    """Return frontmatter dict, or None if missing/unparseable."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None, text
    if not isinstance(data, dict):
        return None, text
    return data, text


# ---------------------------------------------------------------- sync

def parse_repo_url(repo):
    m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo)
    if not m:
        fail(f"cannot parse github repo url: {repo}")
    return m.group(1), m.group(2)


def ls_remote(repo, ref):
    result = subprocess.run(
        ["git", "ls-remote", repo, ref],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail(f"git ls-remote failed for {repo} {ref}: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            return parts[0]
    fail(f"ref {ref} not found in {repo}")


def load_lock():
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def sync():
    lock = load_lock()
    for source in load_sources():
        sid = source["id"]
        repo = source["repo"]
        ref = source.get("ref") or "main"
        sha = ls_remote(repo, ref)
        if lock.get(sid, {}).get("sha") == sha:
            log(f"[sync] {sid}: up to date ({sha[:12]})")
            continue
        log(f"[sync] {sid}: {lock.get(sid, {}).get('sha', 'new')[:12] if sid in lock else 'new'}"
            f" -> {sha[:12]}")

        owner, reponame = parse_repo_url(repo)
        url = f"https://codeload.github.com/{owner}/{reponame}/tar.gz/{sha}"
        dest = os.path.join(VENDOR_DIR, sid)
        with tempfile.TemporaryDirectory() as tmp:
            tarball = os.path.join(tmp, "src.tar.gz")
            try:
                urllib.request.urlretrieve(url, tarball)
            except Exception as e:
                fail(f"[sync] {sid}: download failed: {e}")
            with tarfile.open(tarball, "r:gz") as tf:
                tf.extractall(tmp, filter="data")
            # tarball unpacks into a single top-level dir <reponame>-<sha>/
            tops = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
            if len(tops) != 1:
                fail(f"[sync] {sid}: unexpected tarball layout: {tops}")
            base = os.path.join(tmp, tops[0])
            subdir = source.get("subdir")
            search_root = os.path.join(base, subdir) if subdir else base
            if not os.path.isdir(search_root):
                fail(f"[sync] {sid}: subdir {subdir!r} not found in upstream")
            pattern = source.get("discover") or "*/SKILL.md"
            found = {}
            for skill_md in glob.glob(
                os.path.join(search_root, pattern), recursive=True
            ):
                name = os.path.basename(os.path.dirname(skill_md))
                found[name] = os.path.dirname(skill_md)
            include = source.get("include") or []
            exclude = source.get("exclude") or []
            if include:
                for name in include:
                    if name not in found:
                        log(f"[sync] {sid}: warning: include references "
                            f"undiscovered skill {name!r}")
                selected = [n for n in include if n in found]
            else:
                selected = sorted(found)
            selected = [n for n in selected if n not in exclude]

            # full rebuild of vendor/<source>/
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            os.makedirs(dest)
            for name in selected:
                shutil.copytree(found[name], os.path.join(dest, name))
            # upstream license files -> vendor/<source>/
            for f in os.listdir(base):
                if f.upper().startswith(
                    tuple(p.upper() for p in LICENSE_PATTERNS)
                ) and os.path.isfile(os.path.join(base, f)):
                    shutil.copy2(os.path.join(base, f), os.path.join(dest, f))

        lock[sid] = {
            "repo": repo,
            "ref": ref,
            "sha": sha,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        save_json(LOCK_FILE, lock)
        report = {}
        if os.path.exists(REPORT_FILE):
            with open(REPORT_FILE, "r", encoding="utf-8") as f:
                report = json.load(f)
        report[sid] = {
            "sha": sha,
            "discovered": sorted(found),
            "included": sorted(selected),
        }
        save_json(REPORT_FILE, report)
        log(f"[sync] {sid}: vendored {len(selected)} skill(s): {', '.join(sorted(selected))}")


# ---------------------------------------------------------------- validate

def validate():
    catalogs = load_catalogs()
    problems = []

    # references resolve
    for cname, cat in iter_catalogs(catalogs):
        for entry in cat.get("skills") or []:
            skill_id = entry["id"]
            source, name = parse_ref(skill_id)
            d = resolve_skill_dir(source, name)
            skill_md = os.path.join(d, "SKILL.md")
            if not os.path.isfile(skill_md):
                problems.append(
                    f"[{cname}] dangling reference: {skill_id} "
                    f"({skill_md} not found)"
                )

    # frontmatter parseable
    checked = set()
    for cname, cat in iter_catalogs(catalogs):
        for entry in cat.get("skills") or []:
            source, name = parse_ref(entry["id"])
            d = resolve_skill_dir(source, name)
            skill_md = os.path.join(d, "SKILL.md")
            if skill_md in checked or not os.path.isfile(skill_md):
                continue
            checked.add(skill_md)
            fm, _ = parse_frontmatter(skill_md)
            if fm is None:
                problems.append(f"[{cname}] unparseable frontmatter: {skill_md}")

    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        sys.exit(1)
    log("[validate] ok")


# ---------------------------------------------------------------- build

def merge_metadata(base, override):
    """Shallow-merge metadata dicts; override wins at key level."""
    merged = dict(base or {})
    for k, v in (override or {}).items():
        merged[k] = v
    return merged


def rewrite_frontmatter(skill_md, source_overrides, catalog_override):
    with open(skill_md, "r", encoding="utf-8") as f:
        text = f.read()
    m = FRONTMATTER_RE.match(text)
    if not m:
        fail(f"no frontmatter in {skill_md}")
    fm = yaml.safe_load(m.group(1))
    meta = fm.get("metadata") or {}
    meta = merge_metadata(meta, source_overrides.get("metadata"))
    meta = merge_metadata(meta, (catalog_override or {}).get("metadata"))
    if meta:
        fm["metadata"] = meta
    elif "metadata" in fm:
        del fm["metadata"]
    body = text[m.end():]
    out = io.StringIO()
    yaml.safe_dump(fm, out, sort_keys=False, allow_unicode=True)
    return f"---\n{out.getvalue()}---\n{body}"


def walk_files(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            files.append(os.path.relpath(full, root))
    return sorted(files)


def content_version(root, files):
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode("utf-8"))
        with open(os.path.join(root, rel), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def publish_name(source, name):
    return name if source is None else f"{source}-{name}"


def copy_license(dest_dir, source):
    if source is None:
        return
    src_vendor = os.path.join(VENDOR_DIR, source)
    if not os.path.isdir(src_vendor):
        return
    has_license = any(
        f.upper().startswith(tuple(p.upper() for p in LICENSE_PATTERNS))
        for f in os.listdir(dest_dir)
    )
    if has_license:
        return
    for f in sorted(os.listdir(src_vendor)):
        full = os.path.join(src_vendor, f)
        if (
            os.path.isfile(full)
            and f.upper().startswith(tuple(p.upper() for p in LICENSE_PATTERNS))
        ):
            shutil.copy2(full, os.path.join(dest_dir, f))
            log(f"[build]   copied license {f} -> {dest_dir}")


def build():
    validate()
    sources = {s["id"]: s for s in load_sources()}
    catalogs = load_catalogs()

    if os.path.isdir(DIST_DIR):
        shutil.rmtree(DIST_DIR)
    os.makedirs(DIST_DIR)

    for cname, cat in iter_catalogs(catalogs):
        cpath = os.path.join(DIST_DIR, cat["path"])
        os.makedirs(cpath, exist_ok=True)
        index = []
        for entry in cat.get("skills") or []:
            source, name = parse_ref(entry["id"])
            src = resolve_skill_dir(source, name)
            pname = publish_name(source, name)
            dest = os.path.join(cpath, pname)
            shutil.copytree(src, dest)
            copy_license(dest, source)
            src_overrides = {}
            if source is not None:
                src_overrides = (sources[source].get("overrides") or {}).get(name) or {}
            rewritten = rewrite_frontmatter(
                os.path.join(dest, "SKILL.md"), src_overrides,
                entry.get("metadata") and {"metadata": entry["metadata"]},
            )
            with open(os.path.join(dest, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(rewritten)
            files = walk_files(dest)
            version = content_version(dest, files)
            index.append({"name": pname, "version": version, "files": files})
        # self-check: every listed file exists
        for item in index:
            for rel in item["files"]:
                if not os.path.isfile(os.path.join(cpath, item["name"], rel)):
                    fail(f"[build] self-check failed: missing file "
                         f"{item['name']}/{rel} in {cat['path']}")
        index.sort(key=lambda x: x["name"])
        save_json(os.path.join(cpath, "index.json"), {"skills": index})
        log(f"[build] {cat['path']}: {len(index)} skill(s) published")


def main():
    ap = argparse.ArgumentParser(description="skills catalog tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    sub.add_parser("validate")
    sub.add_parser("build")
    args = ap.parse_args()
    if args.cmd == "sync":
        sync()
    elif args.cmd == "validate":
        validate()
    elif args.cmd == "build":
        build()


if __name__ == "__main__":
    main()

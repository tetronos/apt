#!/usr/bin/env python3
"""build-repo.py: build the tetronos apt repository from the .deb files attached to the suite's GitHub releases.

Nothing is rebuilt: each listed repo's latest N releases (not drafts, not pre-releases) are downloaded, checked
against their .sha256 files when present, placed in pool/, indexed with apt-ftparchive and, with --key, signed.

Usage:
  build-repo.py --out DIR [--sources sources.txt] [--lists lists.txt] [--keep 2] [--cache DIR] [--key KEYID]
                [--base-url URL]

  --key KEYID   sign InRelease and Release.gpg with this key (gpg uses $GNUPGHOME) and export its public
                key as tetronos-archive-keyring.gpg (binary) and .asc. Without --key the repo is unsigned
                (for local checks only; apt refuses it).
  GITHUB_TOKEN  optional; raises the GitHub API rate limit (set in CI).

  --lists FILE  app lists to publish, one "<major.minor> <owner/repo> <git ref>" per line: the repo's
                assets/catalog.json at that ref goes to lists/<major.minor>/ when its Cargo.toml version has
                that minor and the catalog checks out; otherwise that line is skipped with a warning (the apt
                repo is published either way, and tetron-wm keeps its built-in list).

Layout:
  lists/<major.minor>/{catalog.json,suite-apps.txt,SHA256SUMS[,SHA256SUMS.asc]}
  dists/stable/{InRelease,Release,Release.gpg}
  dists/stable/main/binary-{amd64,arm64}/Packages{,.gz,.xz}
  pool/main/<first letter>/<package>/<file>.deb
Needs: python3, dpkg-deb, apt-ftparchive (apt-utils), gzip, xz, gpg (only with --key).
"""
import argparse, fnmatch, gzip, hashlib, html, json, lzma, os, re, shutil, subprocess, sys, urllib.request

ARCHES = ["amd64", "arm64"]
SUITE, COMPONENT = "stable", "main"


def api(path):
    req = urllib.request.Request("https://api.github.com/" + path, headers={"Accept": "application/vnd.github+json"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def fetch(url, dest):
    if os.path.exists(dest):
        return
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def field(deb, name):
    return subprocess.run(["dpkg-deb", "-f", deb, name], capture_output=True, text=True, check=True).stdout.strip()


def collect(sources, keep, cache):
    """sources: (owner/repo, globs) pairs; with globs, only .deb names matching one of them are taken."""
    debs = []
    for repo, globs in sources:
        rels = [r for r in api(f"repos/{repo}/releases?per_page=30") if not r["draft"] and not r["prerelease"]]
        used = 0
        for rel in rels:
            assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
            names = sorted(n for n in assets if n.endswith(".deb")
                           and (not globs or any(fnmatch.fnmatchcase(n, g) for g in globs)))
            if not names:
                continue
            sums = {}  # a release-wide SHA256SUMS file ("<hash>  <name>" lines), e.g. tetron-os
            if "SHA256SUMS" in assets:
                sumsfile = os.path.join(cache, f"{repo.replace('/', '_')}_{rel['tag_name']}_SHA256SUMS")
                fetch(assets["SHA256SUMS"], sumsfile)
                for line in open(sumsfile):
                    parts = line.split()
                    if len(parts) == 2:
                        sums[parts[1].lstrip("*")] = parts[0]
            for n in names:
                dest = os.path.join(cache, n)
                fetch(assets[n], dest)
                want = sums.get(n)
                if n + ".sha256" in assets:
                    sumfile = dest + ".sha256"
                    fetch(assets[n + ".sha256"], sumfile)
                    want = open(sumfile).read().split()[0]
                if want is None:
                    print(f"  note: {repo} {rel['tag_name']} {n} has no published checksum", file=sys.stderr)
                elif sha256(dest) != want:
                    sys.exit(f"checksum mismatch: {repo} {rel['tag_name']} {n}")
                debs.append((repo, rel["tag_name"], dest))
            used += 1
            if used == keep:
                break
        print(f"{repo}: {used} release(s) with .deb files", file=sys.stderr)
    return debs


LIST_SCHEMA = 1  # the catalog layout tetron-wm reads; a new layout gets a new core minor


def raw(repo, ref, path):
    with urllib.request.urlopen(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}", timeout=60) as r:
        return r.read()


def list_problem(minor, cargo, catalog):
    """Why this catalog must not be published for `minor`, or None."""
    m = re.search(r'^version\s*=\s*"(\d+\.\d+)\.', cargo, re.M)
    if not m or m.group(1) != minor:
        return f"its Cargo.toml version is {m.group(1) + '.x' if m else 'unknown'}, not {minor}.x"
    try:
        doc = json.loads(catalog)
    except ValueError as e:
        return f"catalog.json does not parse ({e})"
    if not isinstance(doc, dict) or doc.get("schema") != LIST_SCHEMA:
        return f"catalog.json schema is {doc.get('schema') if isinstance(doc, dict) else 'missing'}, not {LIST_SCHEMA}"
    if not doc.get("apps") or not any(":" in l for l in str(doc.get("suite_apps", "")).splitlines()):
        return "catalog.json has no apps or no suite app list"
    return None


def publish_lists(lists, out, fetch_raw=raw):
    """lists: (minor, repo, ref) triples. Returns the minors published."""
    done = []
    for minor, repo, ref in lists:
        try:
            cargo = fetch_raw(repo, ref, "Cargo.toml").decode()
            catalog = fetch_raw(repo, ref, "assets/catalog.json")
        except OSError as e:
            print(f"  warning: lists {minor}: {repo}@{ref} not fetched ({e}); skipped", file=sys.stderr)
            continue
        why = list_problem(minor, cargo, catalog)
        if why:
            print(f"  warning: lists {minor}: {repo}@{ref} skipped: {why}", file=sys.stderr)
            continue
        d = os.path.join(out, "lists", minor)
        os.makedirs(d, exist_ok=True)
        files = {"catalog.json": catalog, "suite-apps.txt": json.loads(catalog)["suite_apps"].encode()}
        for name, data in files.items():
            open(os.path.join(d, name), "wb").write(data)
        open(os.path.join(d, "SHA256SUMS"), "w").write(
            "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()))
        done.append(minor)
        print(f"lists {minor}: {repo}@{ref}", file=sys.stderr)
    return done


def sign_lists(out, minors, key):
    g = ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--local-user", key]
    pw = os.environ.get("APT_SIGNING_PASSPHRASE")
    if pw:
        g += ["--passphrase-fd", "0"]
    for minor in minors:
        f = os.path.join(out, "lists", minor, "SHA256SUMS")
        subprocess.run(g + ["--detach-sign", "--armor", "--digest-algo", "SHA512", "-o", f + ".asc", f],
                       input=(pw or "").encode(), check=True, capture_output=True)


def place(debs, out):
    seen = {}
    for repo, tag, path in debs:
        pkg, ver, arch = field(path, "Package"), field(path, "Version"), field(path, "Architecture")
        key = (pkg, ver, arch)
        digest = sha256(path)
        if key in seen:
            if seen[key] != digest:
                sys.exit(f"two different files claim {pkg} {ver} {arch} ({repo} {tag})")
            continue
        seen[key] = digest
        d = os.path.join(out, "pool", COMPONENT, pkg[0], pkg)
        os.makedirs(d, exist_ok=True)
        shutil.copy2(path, os.path.join(d, os.path.basename(path)))
    return seen


def index(out):
    for arch in ARCHES:
        d = os.path.join(out, "dists", SUITE, COMPONENT, f"binary-{arch}")
        os.makedirs(d, exist_ok=True)
        pk = subprocess.run(["apt-ftparchive", "--arch", arch, "packages", f"pool/{COMPONENT}"], cwd=out,
                            capture_output=True, check=True).stdout
        open(os.path.join(d, "Packages"), "wb").write(pk)
        with gzip.open(os.path.join(d, "Packages.gz"), "wb", 9) as f:
            f.write(pk)
        with lzma.open(os.path.join(d, "Packages.xz"), "wb") as f:
            f.write(pk)
    opts = {"Origin": "tetronos", "Label": "tetronos", "Suite": SUITE, "Codename": SUITE,
            "Architectures": " ".join(ARCHES), "Components": COMPONENT,
            "Description": "tetron suite and TetronOS packages"}
    args = ["apt-ftparchive"] + [f"-oAPT::FTPArchive::Release::{k}={v}" for k, v in opts.items()] + ["release", "."]
    rel = subprocess.run(args, cwd=os.path.join(out, "dists", SUITE), capture_output=True, check=True).stdout
    open(os.path.join(out, "dists", SUITE, "Release"), "wb").write(rel)


def sign(out, key):
    d = os.path.join(out, "dists", SUITE)
    g = ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--local-user", key]
    pw = os.environ.get("APT_SIGNING_PASSPHRASE")
    if pw:
        g += ["--passphrase-fd", "0"]
    run = lambda a: subprocess.run(g + a, input=(pw or "").encode(), check=True, capture_output=True)
    run(["--clearsign", "--digest-algo", "SHA512", "-o", os.path.join(d, "InRelease"), os.path.join(d, "Release")])
    run(["--detach-sign", "--armor", "--digest-algo", "SHA512", "-o", os.path.join(d, "Release.gpg"), os.path.join(d, "Release")])
    pub = subprocess.run(["gpg", "--batch", "--export", key], capture_output=True, check=True).stdout
    open(os.path.join(out, "tetronos-archive-keyring.gpg"), "wb").write(pub)
    asc = subprocess.run(["gpg", "--batch", "--armor", "--export", key], capture_output=True, check=True).stdout
    open(os.path.join(out, "tetronos-archive-keyring.asc"), "wb").write(asc)


def page(out, base, seen, minors=()):
    rows = "\n".join(f"<tr><td>{html.escape(p)}</td><td>{html.escape(v)}</td><td>{html.escape(a)}</td></tr>"
                     for (p, v, a) in sorted(seen))
    src = (f"Types: deb\nURIs: {base}\nSuites: {SUITE}\nComponents: {COMPONENT}\n"
           f"Architectures: {' '.join(ARCHES)}\n"
           f"Signed-By: /usr/share/keyrings/tetronos-archive-keyring.gpg\n")
    open(os.path.join(out, "tetronos.sources"), "w").write(src)
    open(os.path.join(out, "index.html"), "w").write(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>tetronos apt repository</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 16px}}
pre{{background:#f2f4f7;padding:10px 12px;overflow-x:auto}}td{{padding:2px 12px 2px 0;font-family:monospace}}
@media (prefers-color-scheme: dark){{body{{background:#0e1116;color:#e6eaf0}}pre{{background:#161b22}}a{{color:#8fb8dc}}}}</style>
</head><body>
<h1>tetronos apt repository</h1>
<p>Signed Debian packages of the tetron suite, for Debian 12+ and Ubuntu 22.04+ on amd64 and arm64.
The latest and the previous release of each package are kept, so <code>apt install pkg=&lt;version&gt;</code> can go back one release.</p>
<h2>Add it</h2>
<pre>sudo curl -fsSLo /usr/share/keyrings/tetronos-archive-keyring.gpg {base}/tetronos-archive-keyring.gpg
sudo curl -fsSLo /etc/apt/sources.list.d/tetronos.sources {base}/tetronos.sources
sudo apt update
sudo apt install tetron</pre>
{lists_html(base, minors)}<h2>Packages</h2>
<table><tr><th align=left>package</th><th align=left>version</th><th align=left>arch</th></tr>
{rows}
</table>
<p><a href="https://github.com/tetronos/apt">github.com/tetronos/apt</a></p>
</body></html>
""")
    open(os.path.join(out, ".nojekyll"), "w").write("")


def lists_html(base, minors):
    if not minors:
        return ""
    items = "".join(f'<li><a href="{base}/lists/{m}/catalog.json">{m}</a> '
                    f'(<a href="{base}/lists/{m}/suite-apps.txt">suite-apps.txt</a>)</li>' for m in minors)
    return ("<h2>App lists</h2>\n<p>The tetron-wm app catalog for each core minor version, read by the Store's "
            f"Update list button and the suite installer.</p>\n<ul>{items}</ul>\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--sources", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.txt"))
    ap.add_argument("--lists", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "lists.txt"))
    ap.add_argument("--keep", type=int, default=2)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--key")
    ap.add_argument("--base-url", default="https://tetronos.github.io/apt")
    a = ap.parse_args()
    lines = [l.split("#")[0].split() for l in open(a.sources)]
    sources = [(l[0], l[1:]) for l in lines if l]
    lists = []
    if os.path.exists(a.lists):
        for l in (l.split("#")[0].split() for l in open(a.lists)):
            if l and len(l) != 3:
                sys.exit(f"{a.lists}: want '<major.minor> <owner/repo> <ref>', got {' '.join(l)}")
            if l and any(x[0] == l[0] for x in lists):
                sys.exit(f"{a.lists}: {l[0]} is listed twice")
            if l:
                lists.append(tuple(l))
    if os.path.exists(a.out) and os.listdir(a.out):
        sys.exit(f"--out {a.out} is not empty")
    os.makedirs(a.out, exist_ok=True)
    cache = a.cache or os.path.join(a.out, ".cache")
    os.makedirs(cache, exist_ok=True)
    seen = place(collect(sources, a.keep, cache), a.out)
    if not a.cache:
        shutil.rmtree(cache)
    index(a.out)
    minors = publish_lists(lists, a.out)
    if a.key:
        sign(a.out, a.key)
        sign_lists(a.out, minors, a.key)
    page(a.out, a.base_url.rstrip("/"), seen, minors)
    print(f"{len(seen)} package files in {a.out}{'' if a.key else ' (UNSIGNED)'}", file=sys.stderr)


if __name__ == "__main__":
    main()

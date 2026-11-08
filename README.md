# tetronos apt repository

Signed apt repository for the tetron suite, served at https://tetronos.github.io/apt/.

No packages live in this git repo. `build-repo.py` downloads the `.deb` files attached to the latest two releases of each repo in `sources.txt`, checks them against their `.sha256` files, indexes them with `apt-ftparchive` (amd64 and arm64, one suite `stable`, component `main`) and signs the index. The `Publish` workflow runs it on every push, daily, and when a suite repo sends a `suite-release` event, checks the result with apt in a clean Debian 13 container, and deploys it to GitHub Pages.

Use it:

```
sudo curl -fsSLo /usr/share/keyrings/tetronos-archive-keyring.gpg https://tetronos.github.io/apt/tetronos-archive-keyring.gpg
sudo curl -fsSLo /etc/apt/sources.list.d/tetronos.sources https://tetronos.github.io/apt/tetronos.sources
sudo apt update
sudo apt install tetron
```

Besides the suite, `sources.txt` lists upstream projects whose own `.deb` files are mirrored unchanged (fresh). A line may add globs after the repo so only matching `.deb` names are taken.

Keeping the previous release means `sudo apt install tetron=<previous version>` goes back one release.

Build locally (unsigned unless `--key` is given): `./build-repo.py --out /path/to/out [--key FINGERPRINT]`.

## App lists

`lists.txt` names, per tetron-wm core minor version, the git ref whose `assets/catalog.json` is published at `https://tetronos.github.io/apt/lists/<major.minor>/` as `catalog.json`, `suite-apps.txt` (taken from the catalog) and `SHA256SUMS` (signed as `SHA256SUMS.asc` when the build signs). tetron-wm's Store Update list button and the suite installer read the directory of their own minor, so the app list can change without a tetron-wm release and a list written for a newer core never reaches an older one. A line is skipped with a warning when its ref has another minor in `Cargo.toml` or its catalog does not check out; the apt repository is published either way. When tetron-wm `main` moves to a new minor, point the old minor at its last release tag and add a line for the new one.

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

# gitdir

- Minimal and colorful output 🌈 <img src="https://user-images.githubusercontent.com/27065646/71288165-9914bc80-236a-11ea-853b-a97bff999e79.gif" align="right">
- Works on **Linux**, **MacOS**, and **Windows**
- Support recursive downloading
<br>
<br>
<br>
<br>
<br><br>
<br><br>
<br>

## Install 
```bash
$ pip3 install --user gitdir

# Yes, thats all :)
```

## Usage
```
usage: gitdir [-h] [--output_dir OUTPUT_DIR] [--flatten] [--json] urls [urls ...]

Download directories/folders from GitHub

positional arguments:
  urls                  List of Github directories to download.

optional arguments:
  -h, --help            show this help message and exit
  --output_dir OUTPUT_DIR, -d OUTPUT_DIR
                        All directories will be downloaded to the specified
                        directory.
  --flatten, -f         Flatten directory structures. Do not create extra
                        directory and download found files to output
                        directory. (default to current directory if not
                        specified)
  --json                Emit a deterministic machine-readable report
                        (including the resolved immutable commit) on stdout.
```

Both legacy web URL forms are accepted, including refs that contain a slash
(e.g. `feature/foo`):

```
https://github.com/<owner>/<repo>/tree/<ref>/<dir path>
https://github.com/<owner>/<repo>/blob/<ref>/<file path>
```

## Integrity guarantees

* **Pinned commit.** The named branch/tag is resolved to an immutable commit
  SHA exactly once, before any file is fetched. All later requests use that
  commit's tree SHAs and content-addressed blob SHAs, so a branch advancing
  while a download is in progress can never mix files from different
  revisions. The resolved commit is recorded in the manifest and printed in
  the final report.
* **Verified blobs.** Every blob is fetched by its object SHA and checked
  against (1) the SHA recorded in the commit's tree, (2) its byte size, and
  (3) `sha1("blob " + size + NUL + content)` computed from the received
  bytes. Replaced, truncated, or otherwise mismatching blobs fail the
  download.
* **Atomic publication.** Files are materialized in a hidden staging
  directory and moved to their destination only after the entire target has
  been verified. On any failure nothing is published and the staging area is
  removed. Existing destinations are never overwritten.

## Git object policy

| Mode / type        | Object            | Policy                                                        |
|--------------------|-------------------|---------------------------------------------------------------|
| `040000` tree      | directory         | recreated (including nested trees) and recursed               |
| `100644` blob      | regular file      | written with mode `0644`                                      |
| `100755` blob      | executable file   | written with mode `0755`                                      |
| `120000` blob      | symbolic link     | recreated verbatim with its recorded link target              |
| `160000` commit    | submodule         | **not fetched**; pinned commit (and `.gitmodules` URL) is recorded |

A symbolic link that would shadow another materialized path is rejected
before anything is written. In `--flatten` mode, two repository paths that
map to the same output name are a deterministic error.

## Manifest and report

Every published target carries a deterministic, machine-readable
`gitdir-manifest.json` (single-file and `--flatten` targets get a
`.<name>.gitdir-manifest.json` sidecar). It contains the source URL, the
requested ref and resolved commit, and a path-sorted list of entries with
their kind, mode, object SHA and size. `--json` prints the full report
(including `resolved_commit` and the submodule list) to stdout while progress
goes to stderr.

## Packge Entry

You can use `python -m gitdir` / `python3 -m gitdir` in case the short command does not work.

**Exiting**

To exit the program, just press ```CTRL+C```.

## License
MIT License

Copyright (c) 2019 Siddharth Dushantha

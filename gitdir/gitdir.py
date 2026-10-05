#!/usr/bin/python3
"""Download a single directory/folder (or file) from GitHub.

Integrity model
---------------
A named ref (branch/tag) is resolved exactly once, at planning time, to an
immutable commit SHA.  Every subsequent request is pinned either to that
commit (tree navigation) or to content-addressed object SHAs taken from the
commit's tree (blob downloads).  Advancing the branch while a download is in
progress therefore can never mix files from different revisions.

Every written blob is verified against:

* the blob SHA recorded in the commit's tree, and
* ``sha1("blob " + len + "\\0" + content)`` computed from the received bytes,
* plus the recorded byte size.

Replaced, truncated or otherwise mismatching blobs fail verification and the
target is never published: everything is materialized in a staging directory
and moved into place only after the whole manifest has been verified.

Object policy (documented in README.md)
---------------------------------------
* mode ``040000`` tree        -> recreated as a directory, recursed
* mode ``100644`` blob        -> regular file (0644)
* mode ``100755`` blob        -> executable file (0755)
* mode ``120000`` symlink     -> recreated verbatim as a symbolic link
* mode ``160000`` gitlink     -> submodule is *not* fetched; its pinned commit
                                 is recorded in the manifest/report
"""
import argparse
import base64
import configparser
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from colorama import Fore, Style, init

init()

# this ANSI code lets us erase the current line
ERASE_LINE = "\x1b[2K"

COLOR_NAME_TO_CODE = {"default": "", "red": Fore.RED, "green": Style.BRIGHT + Fore.GREEN}

API_BASE = "https://api.github.com"
MANIFEST_BASENAME = "gitdir-manifest.json"
# GitHub's Git Data API will not serve blobs larger than 100 MiB; such blobs
# cannot be fetched (and therefore cannot be verified) and fail the target.
MAX_BLOB_BYTES = 100 * 1024 * 1024
SCHEMA_VERSION = 1

# Tree entry modes, see https://git-scm.com/book/en/v2/Git-Internals-Git-Objects
MODE_TREE = "040000"
MODE_BLOB = "100644"
MODE_EXEC = "100755"
MODE_SYMLINK = "120000"
MODE_SUBMODULE = "160000"

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

# Legacy GitHub web URLs: https://github.com/<owner>/<repo>/(tree|blob)/<ref>/<path>
_GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/"
    r"(?P<kind>tree|blob)/(?P<rest>[^?#]+?)/?$"
)
_BARE_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+?(?:\.git)?/?$"
)

# Machine-readable output is emitted on stdout; everything else is logged here.
_LOG_STREAM = sys.stdout


class GitdirError(Exception):
    """Expected, user-facing failure (network, API, or verification)."""


class HttpError(GitdirError):
    def __init__(self, status, reason, url):
        self.status = status
        self.reason = reason
        self.url = url
        super().__init__("HTTP {0} for {1}: {2}".format(status, url, reason))


def print_text(text, color="default", in_place=False, **kwargs):
    """print text to console, a wrapper to built-in print"""
    stream = kwargs.pop("file", _LOG_STREAM)
    if in_place:
        print("\r" + ERASE_LINE, end="", file=stream)
    print(COLOR_NAME_TO_CODE[color] + text + Style.RESET_ALL, file=stream, **kwargs)


def log(message, color="default"):
    """Status line; suppressed from stdout in --json mode (goes to stderr)."""
    if _LOG_STREAM is sys.stdout:
        print_text(message, color)
    else:
        print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
# HTTP layer (a module level urlopen hook makes the planner testable offline)
# --------------------------------------------------------------------------- #

def _default_urlopen(request, timeout=30):
    return urllib.request.urlopen(request, timeout=timeout)


_urlopen = _default_urlopen


def _http_get(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "gitdir",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        return _urlopen(request)
    except urllib.error.HTTPError as error:  # carries status, like a response
        raise HttpError(error.code, error.reason, url)
    except urllib.error.URLError as error:
        raise GitdirError("could not reach {0}: {1}".format(url, error.reason))


def api_get_json(url):
    response = _http_get(url)
    try:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))
    finally:
        close = getattr(response, "close", None)
        if close:
            close()


# --------------------------------------------------------------------------- #
# URL parsing / ref resolution
# --------------------------------------------------------------------------- #

def parse_github_url(url):
    """Parse a legacy ``tree``/``blob`` web URL.

    Returns ``(owner, repo, kind, parts)`` where ``parts`` is the ref and the
    in-repo path joined with ``/``.  Both ``tree`` and ``blob`` web URLs keep
    being accepted; only the host form is legacy, the ref itself is resolved to
    an immutable commit by :func:`resolve_ref` before anything is downloaded.
    """
    if _BARE_REPO_RE.match(url):
        raise GitdirError(
            "the given url is a complete repository. Use 'git clone' to "
            "download the repository"
        )
    match = _GITHUB_URL_RE.match(url.rstrip("/"))
    if not match:
        raise GitdirError(
            "not a GitHub tree/blob url (expected "
            "https://github.com/<owner>/<repo>/(tree|blob)/<ref>/<path>): "
            + url
        )
    parts = [p for p in match.group("rest").split("/") if p and p not in (".", "..")]
    if not parts:
        raise GitdirError("the url does not contain a ref: " + url)
    return match.group("owner"), match.group("repo"), match.group("kind"), parts


def _commit_endpoint(owner, repo, ref):
    quoted = urllib.parse.quote(ref, safe="")
    return "{0}/repos/{1}/{2}/commits/{3}".format(API_BASE, owner, repo, quoted)


def resolve_commit(owner, repo, ref):
    """Resolve a ref name/short SHA to ``(commit_sha, root_tree_sha)``."""
    data = api_get_json(_commit_endpoint(owner, repo, ref))
    commit_sha = data.get("sha", "")
    tree_sha = data.get("commit", {}).get("tree", {}).get("sha", "")
    if not _SHA40_RE.match(commit_sha) or not _SHA40_RE.match(tree_sha):
        raise GitdirError("unexpected commit response for ref '{0}'".format(ref))
    return commit_sha, tree_sha


def resolve_ref(owner, repo, kind, parts):
    """Split ``parts`` into an existing ref and an in-repo sub-path.

    Refs may themselves contain ``/`` (e.g. ``feature/foo``); candidates are
    tried shortest-first and the first ref that resolves wins, which is
    deterministic.  Returns ``(requested_ref, path_parts, commit, root_tree)``.
    """
    # A blob URL must leave at least one path component (the file name); a
    # tree URL is allowed to target the repository root.
    max_split = len(parts) if kind == "tree" else len(parts) - 1
    attempts = []
    for split in range(1, max_split + 1):
        ref = "/".join(parts[:split])
        attempts.append(ref)
        try:
            commit_sha, root_tree_sha = resolve_commit(owner, repo, ref)
        except HttpError as error:
            if error.status in (404, 422):
                continue
            raise
        return ref, parts[split:], commit_sha, root_tree_sha
    raise GitdirError(
        "could not resolve any of the candidate refs {0} in {1}/{2}".format(
            attempts, owner, repo
        )
    )


# --------------------------------------------------------------------------- #
# Planning: enumerate the requested subtree at the pinned commit
# --------------------------------------------------------------------------- #

def _tree_url(owner, repo, tree_sha, recursive):
    url = "{0}/repos/{1}/{2}/git/trees/{3}".format(API_BASE, owner, repo, tree_sha)
    return url + "?recursive=1" if recursive else url


def _get_tree(owner, repo, tree_sha, recursive):
    data = api_get_json(_tree_url(owner, repo, tree_sha, recursive))
    if data.get("sha") != tree_sha:
        raise GitdirError(
            "tree SHA mismatch: requested {0}, API returned {1}".format(
                tree_sha, data.get("sha")
            )
        )
    return data.get("tree", []) or []


def _find_tree_child(owner, repo, tree_sha, name):
    for entry in _get_tree(owner, repo, tree_sha, False):
        if entry.get("path") == name:
            if entry.get("mode") != MODE_TREE or entry.get("type") != "tree":
                raise GitdirError(
                    "'{0}' exists but is not a directory (mode {1})".format(
                        name, entry.get("mode")
                    )
                )
            return entry.get("sha")
    raise GitdirError("path component '{0}' not found in tree {1}".format(name, tree_sha))


def _walk_tree(owner, repo, tree_sha, prefix):
    """Non-recursive BFS fallback for when GitHub marks a recursive tree
    response as ``truncated``.  Every subtree is fetched by its own object
    SHA, so the walk cannot drift to a newer commit."""
    collected = []
    stack = [(tree_sha, prefix)]
    while stack:
        current_sha, current_prefix = stack.pop(0)
        for entry in _get_tree(owner, repo, current_sha, False):
            rel = current_prefix + entry["path"]
            item = dict(entry)
            item["path"] = rel
            collected.append(item)
            if entry.get("type") == "tree":
                stack.append((entry["sha"], rel + "/"))
    return collected


def _list_subtree(owner, repo, tree_sha):
    data = api_get_json(_tree_url(owner, repo, tree_sha, True))
    if data.get("sha") != tree_sha:
        raise GitdirError(
            "tree SHA mismatch: requested {0}, API returned {1}".format(
                tree_sha, data.get("sha")
            )
        )
    if data.get("truncated"):
        log("recursive tree listing was truncated; walking trees individually")
        return _walk_tree(owner, repo, tree_sha, "")
    return data.get("tree", []) or []


def classify_entry(entry):
    """Map a raw Git tree entry to a policy kind, validating mode/type."""
    mode = entry.get("mode")
    obj_type = entry.get("type")
    if mode == MODE_TREE:
        expected_type = "tree"
        kind = "tree"
    elif mode == MODE_SUBMODULE:
        expected_type = "commit"
        kind = "submodule"
    elif mode in (MODE_BLOB, MODE_EXEC, MODE_SYMLINK):
        expected_type = "blob"
        kind = {MODE_BLOB: "file", MODE_EXEC: "executable", MODE_SYMLINK: "symlink"}[mode]
    else:
        raise GitdirError(
            "unsupported git mode '{0}' for path '{1}'".format(mode, entry.get("path"))
        )
    if obj_type != expected_type:
        raise GitdirError(
            "object type mismatch at '{0}': mode {1} requires type {2}, got {3}".format(
                entry.get("path"), mode, expected_type, obj_type
            )
        )
    return kind


def plan_entries(owner, repo, root_tree_sha, kind, path_parts):
    """Return ``(entries, repo_prefix)`` pinned to the resolved commit.

    ``entries`` is a deterministically sorted list of::

        {"path", "repo_path", "kind", "mode", "sha", "size"}
    """
    if kind == "blob":
        parent_parts, leaf = path_parts[:-1], path_parts[-1]
        parent_sha = root_tree_sha
        for part in parent_parts:
            parent_sha = _find_tree_child(owner, repo, parent_sha, part)
        raw = None
        for entry in _get_tree(owner, repo, parent_sha, False):
            if entry.get("path") == leaf:
                raw = entry
                break
        if raw is None:
            raise GitdirError("blob path '{0}' not found".format("/".join(path_parts)))
        item_kind = classify_entry(raw)
        if item_kind == "tree":
            raise GitdirError(
                "'{0}' is a directory; use the /tree/ URL form".format(leaf)
            )
        full_path = "/".join(path_parts)
        return [_normalize_entry(raw, leaf, full_path)], os.path.dirname(full_path)

    tree_sha = root_tree_sha
    for part in path_parts:
        tree_sha = _find_tree_child(owner, repo, tree_sha, part)
    repo_prefix = "/".join(path_parts)
    entries = []
    for raw in _list_subtree(owner, repo, tree_sha):
        rel = raw["path"]
        repo_path = repo_prefix + "/" + rel if repo_prefix else rel
        entries.append(_normalize_entry(raw, rel, repo_path))
    entries.sort(key=lambda item: item["path"])
    return entries, repo_prefix


def _normalize_entry(raw, rel_path, repo_path):
    item_kind = classify_entry(raw)
    sha = raw.get("sha", "")
    if not _SHA40_RE.match(sha):
        raise GitdirError("invalid object sha at '{0}': {1}".format(rel_path, sha))
    size = raw.get("size")
    if item_kind in ("file", "executable", "symlink") and not isinstance(size, int):
        raise GitdirError("missing size for blob '{0}'".format(rel_path))
    return {
        "path": rel_path,
        "repo_path": repo_path,
        "kind": item_kind,
        "mode": raw["mode"],
        "sha": sha,
        "size": size,
    }


# --------------------------------------------------------------------------- #
# Blob fetch + digest verification
# --------------------------------------------------------------------------- #

def git_blob_digest(data):
    """sha1("blob " + len + NUL + data), the name Git/GitHub stores blobs by."""
    header = "blob {0}\0".format(len(data)).encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def fetch_blob_bytes(owner, repo, entry):
    """Fetch a blob by its object SHA and verify size and digest.

    Returns the verified bytes.  A replaced, truncated or corrupt blob raises
    GitdirError and nothing is ever written for it.
    """
    path = entry["path"]
    expected_sha = entry["sha"]
    expected_size = entry["size"]
    if expected_size > MAX_BLOB_BYTES:
        raise GitdirError(
            "'{0}' is {1} bytes; GitHub's blob API limit is {2} bytes, so it "
            "cannot be fetched and verified".format(path, expected_size, MAX_BLOB_BYTES)
        )
    data = api_get_json(
        "{0}/repos/{1}/{2}/git/blobs/{3}".format(API_BASE, owner, repo, expected_sha)
    )
    if data.get("sha") != expected_sha:
        raise GitdirError(
            "blob sha mismatch for '{0}': asked {1}, API served {2}".format(
                path, expected_sha, data.get("sha")
            )
        )
    encoding = data.get("encoding")
    content = data.get("content", "")
    if encoding == "base64":
        try:
            raw = base64.b64decode("".join(content.split()), validate=True)
        except Exception as error:
            raise GitdirError("undecodable base64 blob '{0}': {1}".format(path, error))
    elif encoding == "utf-8":
        raw = content.encode("utf-8")
    else:
        raise GitdirError(
            "unsupported blob encoding '{0}' for '{1}'".format(encoding, path)
        )
    if len(raw) != expected_size:
        raise GitdirError(
            "size mismatch for '{0}': manifest records {1} bytes, received {2}".format(
                path, expected_size, len(raw)
            )
        )
    if data.get("size") is not None and data.get("size") != expected_size:
        raise GitdirError("API-reported size mismatch for '{0}'".format(path))
    actual_sha = git_blob_digest(raw)
    if actual_sha != expected_sha:
        raise GitdirError(
            "digest mismatch for '{0}': expected {1}, computed {2} (the blob "
            "was replaced or truncated)".format(path, expected_sha, actual_sha)
        )
    return raw


# --------------------------------------------------------------------------- #
# Submodule metadata (best effort, pinned to the commit)
# --------------------------------------------------------------------------- #

def submodule_urls(owner, repo, commit_sha):
    """Map full submodule paths to the URL recorded in ``.gitmodules``."""
    url = "{0}/repos/{1}/{2}/contents/.gitmodules?ref={3}".format(
        API_BASE, owner, repo, commit_sha
    )
    try:
        data = api_get_json(url)
    except HttpError as error:
        if error.status == 404:
            return {}
        raise
    if data.get("type") != "file" or data.get("encoding") != "base64":
        return {}
    try:
        text = base64.b64decode("".join(data["content"].split())).decode("utf-8")
        parser = configparser.ConfigParser()
        parser.read_string(text)
    except (ValueError, UnicodeDecodeError, configparser.Error):
        return {}
    result = {}
    for section in parser.sections():
        if not section.startswith("submodule"):
            continue
        path = parser.get(section, "path", fallback=None)
        sub_url = parser.get(section, "url", fallback=None)
        if path and sub_url:
            result[path] = sub_url
    return result


# --------------------------------------------------------------------------- #
# Materialization (staging) + atomic publish
# --------------------------------------------------------------------------- #

def _materialized_name(entry, flatten, used_names):
    name = os.path.basename(entry["path"]) if flatten else entry["path"]
    if name in used_names:
        raise GitdirError(
            "flatten target name collision: '{0}' and '{1}' both map to '{2}'".format(
                used_names[name][1], entry["repo_path"], name
            )
        )
    used_names[name] = (entry["kind"], entry["repo_path"])
    return name


def _check_no_symlink_shadow(names):
    """Fail if a symlink would be an ancestor of another materialized path
    (such a tree cannot be checked out safely/deterministically)."""
    symlink_paths = {n for n, (kind, _src) in names.items() if kind == "symlink"}
    for name, (kind, _src) in names.items():
        if kind == "symlink":
            continue
        parent = os.path.dirname(name)
        while parent:
            if parent in symlink_paths:
                raise GitdirError(
                    "symlink '{0}' shadows path '{1}'; refusing to materialize".format(
                        parent, name
                    )
                )
            parent = os.path.dirname(parent)


def materialize(entries, owner, repo, commit_sha, flatten, staging_dir):
    """Fetch, verify and write every entry below ``staging_dir``.

    Returns ``(manifest_entries, stats)``.  Nothing here is visible at the
    final target location; publication is a separate step.
    """
    used_names = {}
    manifest_entries = []
    stats = {"files": 0, "executable": 0, "symlinks": 0, "submodules": 0,
             "total_bytes": 0}
    sub_urls = None
    # Validate the layout before writing anything: a symlink that is an
    # ancestor of another materialized path could otherwise make writes escape
    # the staging directory.
    if not flatten:
        _check_no_symlink_shadow(
            {entry["path"]: (entry["kind"], entry["repo_path"]) for entry in entries}
        )
    for entry in entries:
        kind = entry["kind"]

        if kind == "tree":
            # In flatten mode directory structure is discarded; parents of
            # materialized files are created on demand below.
            if not flatten:
                os.makedirs(os.path.join(staging_dir, entry["path"]), exist_ok=True)
                manifest_entries.append(_manifest_row(entry, entry["path"]))
            continue

        name = _materialized_name(entry, flatten, used_names)

        if kind == "submodule":
            if sub_urls is None:
                sub_urls = submodule_urls(owner, repo, commit_sha)
            stats["submodules"] += 1
            row = _manifest_row(entry, name)
            row["submodule_url"] = sub_urls.get(entry["repo_path"])
            row["submodule_commit"] = entry["sha"]
            manifest_entries.append(row)
            log("submodule (recorded, not fetched): {0} @ {1}".format(
                entry["repo_path"], entry["sha"]))
            continue

        parent = os.path.dirname(os.path.join(staging_dir, name))
        if parent:
            os.makedirs(parent, exist_ok=True)

        raw = fetch_blob_bytes(owner, repo, entry)

        if kind == "symlink":
            # A symlink blob's bytes are the link target, stored verbatim.
            try:
                target = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise GitdirError(
                    "symlink '{0}' target is not valid UTF-8; refusing to "
                    "materialize".format(entry["repo_path"])
                )
            link_path = os.path.join(staging_dir, name)
            try:
                os.symlink(target, link_path)
            except (OSError, NotImplementedError) as error:
                raise GitdirError(
                    "could not create symlink '{0}': {1}".format(entry["repo_path"], error)
                )
            stats["symlinks"] += 1
            log("symlink: {0} -> {1}".format(entry["repo_path"], target))
        else:
            destination = os.path.join(staging_dir, name)
            with open(destination, "wb") as handle:
                handle.write(raw)
            os.chmod(destination, 0o755 if kind == "executable" else 0o644)
            stats["files"] += 1
            if kind == "executable":
                stats["executable"] += 1
            stats["total_bytes"] += len(raw)
            log("verified: {0} ({1} bytes, {2})".format(
                entry["repo_path"], len(raw), entry["mode"]))

        manifest_entries.append(_manifest_row(entry, name))

    manifest_entries.sort(key=lambda row: row["path"])
    return manifest_entries, stats


def _manifest_row(entry, materialized_path):
    return {
        "path": materialized_path,
        "repo_path": entry["repo_path"],
        "kind": entry["kind"],
        "mode": entry["mode"],
        "sha": entry["sha"],
        "size": entry["size"],
    }


def build_manifest(source, manifest_entries, stats):
    return {
        "schema_version": SCHEMA_VERSION,
        "verified": True,
        "source": source,
        "stats": stats,
        "entries": manifest_entries,
    }


def manifest_sidecar_name(kind, path_parts, repo):
    """Deterministic sidecar name for single-file and flatten targets."""
    if kind == "blob":
        leaf = path_parts[-1]
    else:
        leaf = path_parts[-1] if path_parts else repo
    return ".{0}.{1}".format(leaf, MANIFEST_BASENAME)


def publish(staging_dir, output_dir, flatten, kind, path_parts, sidecar_name):
    """Move verified staging content to its final location.

    * Directory targets (a ``tree`` URL without ``--flatten``) are published
      with a single atomic ``rename`` of the staging directory.
    * Single-file and flatten targets pre-check *all* destinations before the
      first move, then move each verified path (same filesystem) atomically.

    Movement only begins after the entire manifest has verified successfully.
    Existing destinations are never overwritten.
    """
    if not flatten and kind == "tree":
        leaf = path_parts[-1] if path_parts else None
        if leaf:
            final_root = os.path.join(output_dir, leaf)
            if os.path.lexists(final_root):
                raise GitdirError(
                    "target already exists (refusing to overwrite): " + final_root
                )
            os.rename(staging_dir, final_root)
            return final_root, os.path.join(final_root, MANIFEST_BASENAME)
        # Whole-repository tree into an existing output directory: pre-check
        # all children, then move them.
        children = os.listdir(staging_dir)
        for name in children:
            dest = os.path.join(output_dir, name)
            if os.path.lexists(dest):
                raise GitdirError(
                    "target already exists (refusing to overwrite): " + dest
                )
        for name in children:
            os.rename(os.path.join(staging_dir, name), os.path.join(output_dir, name))
        return output_dir, os.path.join(output_dir, MANIFEST_BASENAME)

    # File / flatten mode: enumerate every materialized path (os.walk lists
    # symlinks among files); the manifest travels under its sidecar name.
    moves = []
    for root, _dirs, files in os.walk(staging_dir):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), staging_dir)
            if rel == MANIFEST_BASENAME:
                dest = os.path.join(output_dir, sidecar_name)
            else:
                dest = os.path.normpath(os.path.join(output_dir, rel))
            moves.append((rel, dest))
    for _rel, dest in moves:
        if os.path.lexists(dest):
            raise GitdirError("target already exists (refusing to overwrite): " + dest)
    for rel, dest in moves:
        dest_parent = os.path.dirname(dest)
        if dest_parent:
            os.makedirs(dest_parent, exist_ok=True)
        os.rename(os.path.join(staging_dir, rel), dest)
    return output_dir, os.path.join(output_dir, sidecar_name)


# --------------------------------------------------------------------------- #
# Top level orchestration
# --------------------------------------------------------------------------- #

def download(repo_url, flatten=False, output_dir="./"):
    """Plan, verify and publish the GitHub target behind ``repo_url``.

    Returns a deterministic, machine-readable report dict that always includes
    the resolved immutable commit SHA.  Raises GitdirError on any failure;
    on failure no verified target is published.
    """
    owner, repo, kind, parts = parse_github_url(repo_url)
    requested_ref, path_parts, commit_sha, root_tree_sha = resolve_ref(
        owner, repo, kind, parts
    )
    log("resolved ref '{0}' -> commit {1}".format(requested_ref, commit_sha), "green")

    entries, repo_prefix = plan_entries(owner, repo, root_tree_sha, kind, path_parts)

    os.makedirs(output_dir, exist_ok=True)
    staging_dir = tempfile.mkdtemp(prefix=".gitdir-staging-", dir=output_dir)
    try:
        manifest_rows, stats = materialize(
            entries, owner, repo, commit_sha, flatten, staging_dir
        )
        source = {
            "provider": "github",
            "url": repo_url,
            "owner": owner,
            "repo": repo,
            "kind": kind,
            "requested_ref": requested_ref,
            "resolved_commit": commit_sha,
            "requested_path": "/".join(path_parts),
        }
        manifest = build_manifest(source, manifest_rows, stats)
        rendered = json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        with open(os.path.join(staging_dir, MANIFEST_BASENAME), "w", encoding="utf-8") as handle:
            handle.write(rendered)

        sidecar = manifest_sidecar_name(kind, path_parts, repo)
        target_dir, manifest_path = publish(
            staging_dir, output_dir, flatten, kind, path_parts, sidecar
        )
        # Directory-mode publication renames the staging dir away; file-mode
        # publication leaves the now-empty staging directory behind.
        shutil.rmtree(staging_dir, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    report = {
        "ok": True,
        "url": repo_url,
        "owner": owner,
        "repo": repo,
        "kind": kind,
        "requested_ref": requested_ref,
        "resolved_commit": commit_sha,
        "requested_path": "/".join(path_parts),
        "target": os.path.abspath(target_dir),
        "manifest": os.path.abspath(manifest_path),
        "stats": stats,
        "submodules": [
            {
                "path": row["repo_path"],
                "commit": row["submodule_commit"],
                "url": row.get("submodule_url"),
            }
            for row in manifest_rows if row["kind"] == "submodule"
        ],
    }
    return report


def _print_report(report):
    print_text("✔ Verified and published: " + Fore.WHITE + report["target"], "green")
    print_text("  resolved commit : " + Fore.WHITE + report["resolved_commit"])
    print_text("  requested ref  : " + Fore.WHITE + report["requested_ref"])
    stats = report["stats"]
    print_text(
        "  files           : {0} ({1} executable), symlinks: {2}, submodules: {3}".format(
            stats["files"], stats["executable"], stats["symlinks"], stats["submodules"]
        )
    )
    for sub in report["submodules"]:
        print_text("  submodule      : {0} @ {1}".format(sub["path"], sub["commit"]))
    print_text("  manifest       : " + Fore.WHITE + report["manifest"])


def main():
    global _LOG_STREAM

    if sys.platform != 'win32':
        # disbale CTRL+Z
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)

    parser = argparse.ArgumentParser(description="Download directories/folders from GitHub")
    parser.add_argument('urls', nargs="+",
                        help="List of Github directories to download.")
    parser.add_argument('--output_dir', "-d", dest="output_dir", default="./",
                        help="All directories will be downloaded to the specified directory.")
    parser.add_argument('--flatten', '-f', action="store_true",
                        help='Flatten directory structures. Do not create extra directory and download found files to'
                             ' output directory. (default to current directory if not specified)')
    parser.add_argument('--json', dest="json_mode", action="store_true",
                        help="Emit a deterministic machine-readable report (incl. the resolved commit) on stdout.")

    args = parser.parse_args()

    if args.json_mode:
        _LOG_STREAM = sys.stderr

    results = []
    failed = 0
    for url in args.urls:
        try:
            report = download(url, flatten=args.flatten, output_dir=args.output_dir)
        except GitdirError as error:
            failed += 1
            results.append({"ok": False, "url": url, "error": str(error)})
            print_text("✘ {0}: {1}".format(url, error), "red", file=sys.stderr)
            continue
        except KeyboardInterrupt:
            print_text("✘ Got interrupted", "red", in_place=False, file=sys.stderr)
            sys.exit(130)
        results.append(report)
        if not args.json_mode:
            _print_report(report)

    if args.json_mode:
        json.dump({"results": results}, sys.stdout, sort_keys=True, indent=2)
        sys.stdout.write("\n")
    elif failed == 0:
        print_text("✔ Download complete", "green", in_place=True)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

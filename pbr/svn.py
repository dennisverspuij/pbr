# Copyright 2011 OpenStack Foundation
# Copyright 2012-2013 Hewlett-Packard Development Company, L.P.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from __future__ import unicode_literals

from distutils import log
import errno
import os
import re
import time
import xml.etree.ElementTree as ET

from pbr import options
from pbr import version
from pbr.git import _run_shell_command, _get_author_filter


def _run_svn_command(cmd, **kwargs):
    if not isinstance(cmd, (list, tuple)):
        cmd = [cmd]
    return _run_shell_command(['svn'] + cmd, **kwargs)


def _get_svn_directory():
    try:
        return _run_svn_command(['--show-item', 'wc-root', 'info'])
    except OSError as e:
        if e.errno == errno.ENOENT:
            # svn not installed.
            return ''
        raise


def _svn_is_installed():
    try:
        # We cannot use 'which svn' as it may not be available
        # in some distributions, So just try 'svn --version'
        # to see if we run into trouble
        _run_svn_command(['--version'])
    except OSError:
        return False
    return True


def _find_svn_files(dirname='', svn_dir=None):
    """Behave like a file finder entrypoint plugin.

    We don't actually use the entrypoints system for this because it runs
    at absurd times. We only want to do this when we are building an sdist.
    """
    if svn_dir is None:
        svn_dir = _run_svn_functions()
        if svn_dir is None:
            return []
    log.info("[pbr] In svn context, generating filelist from svn")
    return [f for f in _run_svn_command(['list', '--recursive'], cwd=svn_dir, throw_on_error=True).splitlines() if f and f[-1] != u'/']


def _run_svn_functions():
    svn_dir = None
    if _svn_is_installed():
        svn_dir = _get_svn_directory()
    return svn_dir or None


def _iter_log_oneline(svn_dir=None):
    """Iterate over --oneline log entries if possible.

    This parses the output into a structured form but does not apply
    presentation logic to the output - making it suitable for different
    uses.

    :return: An iterator of (hash, tags_set, 1st_line) tuples, or None if
        changelog generation is disabled / not available.
    """
    if svn_dir is None:
        svn_dir = _get_svn_directory()
    if not svn_dir:
        return []
    return _iter_log_inner(svn_dir)


def _numeric_revision(svn_dir, rev):
    """Returns for the SVN repository associated with the working copy in given svn_dir
    the revision number corresponding to given rev, which may already be a number or a
    string with a number, BASE, HEAD, COMMITTED, PREV or {DATE}.
    Raises an exception if given revision does not exist.
    """
    return rev if isinstance(rev, int) else int(_run_svn_command(['info', '--show-item', 'revision', '-r', rev], cwd=svn_dir, throw_on_error=True), 10)


def _iter_log_xml(svn_dir, start_rev, end_rev, args=(), chunksize=250):
    """Iterate over the log entries in range start_rev:end_rev of given SVN WC svn_dir.
    Given args (options and optional trailing ^/in/repo/path) are appended to the svn log command.

    The log is processed in chunks of 250 log entries to avoid memory exhaustion.
    Depending on the additional options you provide you may wish to provide a different chunksize,
    e.g. higher to speed up, or 1 if you want the first entry only.

    :return: An iterator of (revision, xml.etree.ElementTree.Element) tuples
    """
    start_rev = _numeric_revision(svn_dir, start_rev)
    end_rev = _numeric_revision(svn_dir, end_rev)
    increment = 1 if start_rev <= end_rev else -1
    args = ['--xml', '-l', str(max(1, chunksize))] + list(args)
    while True:
        rev = None
        for logentry in ET.fromstring(_run_svn_command(['log', '-r', '%d:%d' % (start_rev, end_rev)] + args, cwd=svn_dir, throw_on_error=True)):
            rev = int(logentry.attrib['revision'], 10)
            yield rev, logentry
        if rev is None or rev == end_rev:
            break
        start_rev = rev + increment


def _escape_xpath_string(str):
    """Returns given string as a quoted string expression that can be used inside an Xpath query."""
    str = list(map(lambda s: "'\"'" if s == '"' else '"%s"' % s, filter(None, re.split('(")', str))))
    return str[0] if len(str) == 1 else "concat(%s)" % ','.join(str)


def get_ancestry_and_tags(svn_dir):
    """Returns tuple (ancestry, tags) with meta information about the ancestry,
    and tags created from this ancestry, of given ^path@rev in the SVN repository
    of the working copy in given svn_dir.

    Ancestry will contain [ { path: '/in/repo/path', first_rev: <x>, last_rev: <x> }, ... ]
    in order of the path and revision of the working copy up to the genesis path, with:
    - path      = repository path of the branch
    - first_rev = first revision of the branch
    - last_rev  = last revision of the branch (prior to the copy to later branch)

    Tags will contain [{ path: '/in/repo/path', version: <pbr.version.SemanticVersion>, rev: <x>}, ...]
    in order from latest to oldest tag created in the history of the working copy, with:
    - path      = repository path of the tag
    - version   = version represented by the tag name as pbr.version.SemanticVersion instance
    - rev       = revision the tag was created in
    - from-rev  = last logged revision of the branch the tag was created from
    """

    # Collect ancestry
    path = _run_svn_command(['info', '--show-item', 'relative-url'], cwd=svn_dir, throw_on_error=True)[1:]
    rev = 'BASE'
    ancestry = []
    tags = []
    while True:
        # Append current branch to the ancestry
        first_rev, first_log = next(_iter_log_xml(svn_dir, 1, rev, args=('-v', '--stop-on-copy', '^%s' % path), chunksize=1))
        last_rev, last_log = next(_iter_log_xml(svn_dir, rev, 1, args=('-v', '^%s' % path), chunksize=1))
        ancestry.append({'path': path, 'first_rev': first_rev, 'last_rev': last_rev})

        # Collect tags created from this branch between its first and last revision
        pathq = './paths/path[@kind="dir"][@copyfrom-rev][@copyfrom-path=%s]' % _escape_xpath_string(path)
        for to_rev, logentry in _iter_log_xml(svn_dir, first_rev, last_rev, args=('-q', '-v', '^/')):
            for p in logentry.findall(pathq):
                try:
                    to_path = p.text
                    tags.append({
                      'version': version.SemanticVersion.from_pip_string(to_path.split(u'/')[-1].replace('-', '.')),
                      'path': to_path,
                      'rev': to_rev,
                      'from-rev': next(_iter_log_xml(svn_dir, to_rev, 1, args=('-v', '^%s' % path), chunksize=1))[0]
                    })
                except ValueError:
                    pass   # Path basename cannot be parsed as a semantic version, so it's probably a branch we should skip

        # Step to branch the current was copied from, or break loop when we are in the genesis branch
        try:
            copy = next(iter(list(filter(lambda p: p.text == path, first_log.findall('./paths/path[@kind="dir"][@copyfrom-path]')))))
            path = copy.attrib['copyfrom-path']
            rev = first_rev
        except StopIteration:
            break

    return ancestry, tags


def _iter_log_inner(svn_dir):
    """Iterate over --oneline log entries.

    This parses the output into a structured form but does not apply
    presentation logic to the output - making it suitable for different
    uses.

    :return: An iterator of (revision, tags_set, 1st_line) tuples.
    """
    log.info('[pbr] Generating ChangeLog')
    ancestry, all_tags = get_ancestry_and_tags(svn_dir)
    all_tags.reverse()
    for rev, logentry in _iter_log_xml(svn_dir, 'HEAD', 1, ('--with-all-revprops',)):
        msg = logentry.findtext('./msg', u'').rstrip()
        semver = logentry.find('./revprops/property[@name="sem-ver"]')
        if semver is not None:
            semver = [symbol.strip() for line in semver.text.lower().splitlines() for symbol in line.split(',')]
            if len(semver):
                msg += u'\n(Sem-Ver: %s)' % u', '.join(semver)
        tags = set()
        while (all_tags and all_tags[-1]['from-rev'] >= rev):
            tags.add(all_tags.pop()['version'].release_string())
        yield u'%d' % rev, tags, msg.lstrip().replace(u'\n', u'\n ')


def generate_authors(svn_dir=None, dest_dir='.', option_dict=dict()):
    """Create AUTHORS file using git commits."""
    should_skip = options.get_boolean_option(option_dict, 'skip_authors',
                                             'SKIP_GENERATE_AUTHORS')
    if should_skip:
        return
    start = time.time()
    old_authors = os.path.join(dest_dir, 'AUTHORS.in')
    new_authors = os.path.join(dest_dir, 'AUTHORS')
    # If there's already an AUTHORS file and it's not writable, just use it
    if (os.path.exists(new_authors)
            and not os.access(new_authors, os.W_OK)):
        return
    log.info('[pbr] Generating AUTHORS')
    filter_author = _get_author_filter(option_dict)
    if svn_dir is None:
        svn_dir = _get_svn_directory()
    if svn_dir:
        # Collect (co)authors
        authors = set()
        for rev, logentry in _iter_log_xml(svn_dir, 'HEAD', 1, ('--with-all-revprops',)):
            authors.add(logentry.findtext('./author', u'').strip())
            authors.update(coauthor.strip() for coauthors in logentry.findtext('./revprops/property[@name="co-authored-by"]', u'').splitlines() for coauthor in coauthors.split(u':'))
        authors.discard(u'')

        # convert to list, excluding jenkins email address in AUTHORS file
        authors = sorted(a for a in set(authors) if filter_author(a))
        # TODO: Map the author user names to Name <email@domain.tld> using .mailmap file when available

        with open(new_authors, 'wb') as new_authors_fh:
            if os.path.exists(old_authors):
                with open(old_authors, "rb") as old_authors_fh:
                    new_authors_fh.write(old_authors_fh.read())
            new_authors_fh.write(('\n'.join(authors) + '\n')
                                 .encode('utf-8'))
    stop = time.time()
    log.info('[pbr] AUTHORS complete (%0.1fs)' % (stop - start))

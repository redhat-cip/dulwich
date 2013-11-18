# eventlet.py -- Utility module for querying an ObjectStore with eventlet
# Copyright (C) 2013 eNovance SAS <licensing@enovance.com>
#
# Author: Fabien Boucher <fabien.boucher@enovance.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 2
# of the License or (at your option) any later version of
# the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.

import gevent
from gevent import pool

from dulwich.objects import (
    Commit,
    Tag,
    )
from dulwich.object_store import (
    MissingObjectFinder,
    _collect_filetree_revs,
    ObjectStoreIterator,
    )


def _split_commits_and_tags(obj_store, lst,
                            ignore_unknown=False, pool=None):
    """Split object id list into two list with commit SHA1s and tag SHA1s.

    Same implementation as object_store._split_commits_and_tags
    except we use eventlet to parallelize object retrieval.
    """
    commits = set()
    tags = set()

    def find_commit_type(sha):
        try:
            o = obj_store[sha]
        except KeyError:
            if not ignore_unknown:
                raise
        else:
            if isinstance(o, Commit):
                commits.add(sha)
            elif isinstance(o, Tag):
                tags.add(sha)
                commits.add(o.object[1])
            else:
                raise KeyError('Not a commit or a tag: %s' % sha)
    jobs = [pool.spawn(find_commit_type, s) for s in lst]
    gevent.joinall(jobs)
    return (commits, tags)


class GreenThreadsMissingObjectFinder(MissingObjectFinder):
    """Find the objects missing from another object store.

    Same implementation as object_store.MissingObjectFinder
    except we use eventlet to parallelize object retrieval.
    """
    def __init__(self, object_store, haves, wants,
                 progress=None, get_tagged=None, concurrency=1):

        def collect_tree_sha(sha):
            self.sha_done.add(sha)
            cmt = object_store[sha]
            _collect_filetree_revs(object_store, cmt.tree, self.sha_done)

        self.object_store = object_store
        p = pool.Pool(size=concurrency)

        have_commits, have_tags = \
            _split_commits_and_tags(object_store, haves,
                                    True, p)
        want_commits, want_tags = \
            _split_commits_and_tags(object_store, wants,
                                    False, p)
        all_ancestors = object_store._collect_ancestors(have_commits)[0]
        missing_commits, common_commits = \
            object_store._collect_ancestors(want_commits, all_ancestors)

        self.sha_done = set()
        jobs = [p.spawn(collect_tree_sha, c) for c in common_commits]
        gevent.joinall(jobs)
        for t in have_tags:
            self.sha_done.add(t)
        missing_tags = want_tags.difference(have_tags)
        wants = missing_commits.union(missing_tags)
        self.objects_to_send = set([(w, None, False) for w in wants])
        if progress is None:
            self.progress = lambda x: None
        else:
            self.progress = progress
        self._tagged = get_tagged and get_tagged() or {}


class GreenThreadsObjectStoreIterator(ObjectStoreIterator):
    """ObjectIterator that works on top of an ObjectStore.

    Same implementation as object_store.ObjectStoreIterator
    except we use eventlet to parallelize object retrieval.
    """
    def __init__(self, store, shas, finder, concurrency=1):
        self.finder = finder
        self.p = pool.Pool(size=concurrency)
        super(GreenThreadsObjectStoreIterator, self).__init__(store, shas)

    def retrieve(self, args):
        sha, path = args
        return self.store[sha], path

    def __iter__(self):
        for sha, path in self.p.imap_unordered(self.retrieve,
                                               self.itershas()):
            yield sha, path

    def __len__(self):
        if len(self._shas) > 0:
            return len(self._shas)
        while len(self.finder.objects_to_send):
            jobs = []
            for _ in xrange(0, len(self.finder.objects_to_send)):
                jobs.append(self.p.spawn(self.finder.next))
            gevent.joinall(jobs)
            for j in jobs:
                if j.value is not None:
                    self._shas.append(j.value)
        return len(self._shas)

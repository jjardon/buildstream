#
#  Copyright (C) 2018 Codethink Limited
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	 See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Jürg Billeter <juerg.billeter@codethink.co.uk>

import hashlib
import itertools
import os
import stat
import tempfile
import uuid
import contextlib

import grpc

from .._protos.build.bazel.remote.execution.v2 import remote_execution_pb2
from .._protos.buildstream.v2 import buildstream_pb2

from .. import utils
from .._exceptions import CASCacheError

from .casremote import BlobNotFound, _CASBatchRead, _CASBatchUpdate


# A CASCache manages a CAS repository as specified in the Remote Execution API.
#
# Args:
#     path (str): The root directory for the CAS repository
#
class CASCache():

    def __init__(self, path):
        self.casdir = os.path.join(path, 'cas')
        self.tmpdir = os.path.join(path, 'tmp')
        os.makedirs(os.path.join(self.casdir, 'refs', 'heads'), exist_ok=True)
        os.makedirs(os.path.join(self.casdir, 'objects'), exist_ok=True)
        os.makedirs(self.tmpdir, exist_ok=True)

    # preflight():
    #
    # Preflight check.
    #
    def preflight(self):
        headdir = os.path.join(self.casdir, 'refs', 'heads')
        objdir = os.path.join(self.casdir, 'objects')
        if not (os.path.isdir(headdir) and os.path.isdir(objdir)):
            raise CASCacheError("CAS repository check failed for '{}'".format(self.casdir))

    # contains():
    #
    # Check whether the specified ref is already available in the local CAS cache.
    #
    # Args:
    #     ref (str): The ref to check
    #
    # Returns: True if the ref is in the cache, False otherwise
    #
    def contains(self, ref):
        refpath = self._refpath(ref)

        # This assumes that the repository doesn't have any dangling pointers
        return os.path.exists(refpath)

    # contains_subdir_artifact():
    #
    # Check whether the specified artifact element tree has a digest for a subdir
    # which is populated in the cache, i.e non dangling.
    #
    # Args:
    #     ref (str): The ref to check
    #     subdir (str): The subdir to check
    #
    # Returns: True if the subdir exists & is populated in the cache, False otherwise
    #
    def contains_subdir_artifact(self, ref, subdir):
        tree = self.resolve_ref(ref)

        # This assumes that the subdir digest is present in the element tree
        subdirdigest = self._get_subdir(tree, subdir)
        objpath = self.objpath(subdirdigest)

        # True if subdir content is cached or if empty as expected
        return os.path.exists(objpath)

    # extract():
    #
    # Extract cached directory for the specified ref if it hasn't
    # already been extracted.
    #
    # Args:
    #     ref (str): The ref whose directory to extract
    #     path (str): The destination path
    #     subdir (str): Optional specific dir to extract
    #
    # Raises:
    #     CASCacheError: In cases there was an OSError, or if the ref did not exist.
    #
    # Returns: path to extracted directory
    #
    def extract(self, ref, path, subdir=None):
        tree = self.resolve_ref(ref, update_mtime=True)

        originaldest = dest = os.path.join(path, tree.hash)

        # If artifact is already extracted, check if the optional subdir
        # has also been extracted. If the artifact has not been extracted
        # a full extraction would include the optional subdir
        if os.path.isdir(dest):
            if subdir:
                if not os.path.isdir(os.path.join(dest, subdir)):
                    dest = os.path.join(dest, subdir)
                    tree = self._get_subdir(tree, subdir)
                else:
                    return dest
            else:
                return dest

        with tempfile.TemporaryDirectory(prefix='tmp', dir=self.tmpdir) as tmpdir:
            checkoutdir = os.path.join(tmpdir, ref)
            self._checkout(checkoutdir, tree)

            try:
                utils.move_atomic(checkoutdir, dest)
            except utils.DirectoryExistsError:
                # Another process beat us to rename
                pass
            except OSError as e:
                raise CASCacheError("Failed to extract directory for ref '{}': {}".format(ref, e)) from e

        return originaldest

    # commit():
    #
    # Commit directory to cache.
    #
    # Args:
    #     refs (list): The refs to set
    #     path (str): The directory to import
    #
    def commit(self, refs, path):
        tree = self._commit_directory(path)

        for ref in refs:
            self.set_ref(ref, tree)

    # diff():
    #
    # Return a list of files that have been added or modified between
    # the refs described by ref_a and ref_b.
    #
    # Args:
    #     ref_a (str): The first ref
    #     ref_b (str): The second ref
    #     subdir (str): A subdirectory to limit the comparison to
    #
    def diff(self, ref_a, ref_b, *, subdir=None):
        tree_a = self.resolve_ref(ref_a)
        tree_b = self.resolve_ref(ref_b)

        if subdir:
            tree_a = self._get_subdir(tree_a, subdir)
            tree_b = self._get_subdir(tree_b, subdir)

        added = []
        removed = []
        modified = []

        self._diff_trees(tree_a, tree_b, added=added, removed=removed, modified=modified)

        return modified, removed, added

    # pull():
    #
    # Pull a ref from a remote repository.
    #
    # Args:
    #     ref (str): The ref to pull
    #     remote (CASRemote): The remote repository to pull from
    #     progress (callable): The progress callback, if any
    #     subdir (str): The optional specific subdir to pull
    #     excluded_subdirs (list): The optional list of subdirs to not pull
    #
    # Returns:
    #   (bool): True if pull was successful, False if ref was not available
    #
    def pull(self, ref, remote, *, progress=None, subdir=None, excluded_subdirs=None):
        try:
            remote.init()

            request = buildstream_pb2.GetReferenceRequest(instance_name=remote.spec.instance_name)
            request.key = ref
            response = remote.ref_storage.GetReference(request)

            tree = remote_execution_pb2.Digest()
            tree.hash = response.digest.hash
            tree.size_bytes = response.digest.size_bytes

            # Check if the element artifact is present, if so just fetch the subdir.
            if subdir and os.path.exists(self.objpath(tree)):
                self._fetch_subdir(remote, tree, subdir)
            else:
                # Fetch artifact, excluded_subdirs determined in pullqueue
                self._fetch_directory(remote, tree, excluded_subdirs=excluded_subdirs)

            self.set_ref(ref, tree)

            return True
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise CASCacheError("Failed to pull ref {}: {}".format(ref, e)) from e
            else:
                return False
        except BlobNotFound as e:
            return False

    # pull_tree():
    #
    # Pull a single Tree rather than a ref.
    # Does not update local refs.
    #
    # Args:
    #     remote (CASRemote): The remote to pull from
    #     digest (Digest): The digest of the tree
    #
    def pull_tree(self, remote, digest):
        try:
            remote.init()

            digest = self._fetch_tree(remote, digest)

            return digest

        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise

        return None

    # link_ref():
    #
    # Add an alias for an existing ref.
    #
    # Args:
    #     oldref (str): An existing ref
    #     newref (str): A new ref for the same directory
    #
    def link_ref(self, oldref, newref):
        tree = self.resolve_ref(oldref)

        self.set_ref(newref, tree)

    # push():
    #
    # Push committed refs to remote repository.
    #
    # Args:
    #     refs (list): The refs to push
    #     remote (CASRemote): The remote to push to
    #
    # Returns:
    #   (bool): True if any remote was updated, False if no pushes were required
    #
    # Raises:
    #   (CASCacheError): if there was an error
    #
    def push(self, refs, remote):
        skipped_remote = True
        try:
            for ref in refs:
                tree = self.resolve_ref(ref)

                # Check whether ref is already on the server in which case
                # there is no need to push the ref
                try:
                    request = buildstream_pb2.GetReferenceRequest(instance_name=remote.spec.instance_name)
                    request.key = ref
                    response = remote.ref_storage.GetReference(request)

                    if response.digest.hash == tree.hash and response.digest.size_bytes == tree.size_bytes:
                        # ref is already on the server with the same tree
                        continue

                except grpc.RpcError as e:
                    if e.code() != grpc.StatusCode.NOT_FOUND:
                        # Intentionally re-raise RpcError for outer except block.
                        raise

                self._send_directory(remote, tree)

                request = buildstream_pb2.UpdateReferenceRequest(instance_name=remote.spec.instance_name)
                request.keys.append(ref)
                request.digest.hash = tree.hash
                request.digest.size_bytes = tree.size_bytes
                remote.ref_storage.UpdateReference(request)

                skipped_remote = False
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise CASCacheError("Failed to push ref {}: {}".format(refs, e), temporary=True) from e

        return not skipped_remote

    # push_directory():
    #
    # Push the given virtual directory to a remote.
    #
    # Args:
    #     remote (CASRemote): The remote to push to
    #     directory (Directory): A virtual directory object to push.
    #
    # Raises:
    #     (CASCacheError): if there was an error
    #
    def push_directory(self, remote, directory):
        remote.init()

        self._send_directory(remote, directory.ref)

    # objpath():
    #
    # Return the path of an object based on its digest.
    #
    # Args:
    #     digest (Digest): The digest of the object
    #
    # Returns:
    #     (str): The path of the object
    #
    def objpath(self, digest):
        return os.path.join(self.casdir, 'objects', digest.hash[:2], digest.hash[2:])

    # add_object():
    #
    # Hash and write object to CAS.
    #
    # Args:
    #     digest (Digest): An optional Digest object to populate
    #     path (str): Path to file to add
    #     buffer (bytes): Byte buffer to add
    #     link_directly (bool): Whether file given by path can be linked
    #
    # Returns:
    #     (Digest): The digest of the added object
    #
    # Either `path` or `buffer` must be passed, but not both.
    #
    def add_object(self, *, digest=None, path=None, buffer=None, link_directly=False):
        # Exactly one of the two parameters has to be specified
        assert (path is None) != (buffer is None)

        if digest is None:
            digest = remote_execution_pb2.Digest()

        try:
            h = hashlib.sha256()
            # Always write out new file to avoid corruption if input file is modified
            with contextlib.ExitStack() as stack:
                if path is not None and link_directly:
                    tmp = stack.enter_context(open(path, 'rb'))
                    for chunk in iter(lambda: tmp.read(4096), b""):
                        h.update(chunk)
                else:
                    tmp = stack.enter_context(tempfile.NamedTemporaryFile(dir=self.tmpdir))
                    # Set mode bits to 0644
                    os.chmod(tmp.name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

                    if path:
                        with open(path, 'rb') as f:
                            for chunk in iter(lambda: f.read(4096), b""):
                                h.update(chunk)
                                tmp.write(chunk)
                    else:
                        h.update(buffer)
                        tmp.write(buffer)

                    tmp.flush()

                digest.hash = h.hexdigest()
                digest.size_bytes = os.fstat(tmp.fileno()).st_size

                # Place file at final location
                objpath = self.objpath(digest)
                os.makedirs(os.path.dirname(objpath), exist_ok=True)
                os.link(tmp.name, objpath)

        except FileExistsError as e:
            # We can ignore the failed link() if the object is already in the repo.
            pass

        except OSError as e:
            raise CASCacheError("Failed to hash object: {}".format(e)) from e

        return digest

    # set_ref():
    #
    # Create or replace a ref.
    #
    # Args:
    #     ref (str): The name of the ref
    #
    def set_ref(self, ref, tree):
        refpath = self._refpath(ref)
        os.makedirs(os.path.dirname(refpath), exist_ok=True)
        with utils.save_file_atomic(refpath, 'wb', tempdir=self.tmpdir) as f:
            f.write(tree.SerializeToString())

    # resolve_ref():
    #
    # Resolve a ref to a digest.
    #
    # Args:
    #     ref (str): The name of the ref
    #     update_mtime (bool): Whether to update the mtime of the ref
    #
    # Returns:
    #     (Digest): The digest stored in the ref
    #
    def resolve_ref(self, ref, *, update_mtime=False):
        refpath = self._refpath(ref)

        try:
            with open(refpath, 'rb') as f:
                if update_mtime:
                    os.utime(refpath)

                digest = remote_execution_pb2.Digest()
                digest.ParseFromString(f.read())
                return digest

        except FileNotFoundError as e:
            raise CASCacheError("Attempt to access unavailable ref: {}".format(e)) from e

    # update_mtime()
    #
    # Update the mtime of a ref.
    #
    # Args:
    #     ref (str): The ref to update
    #
    def update_mtime(self, ref):
        try:
            os.utime(self._refpath(ref))
        except FileNotFoundError as e:
            raise CASCacheError("Attempt to access unavailable ref: {}".format(e)) from e

    # calculate_cache_size()
    #
    # Return the real disk usage of the CAS cache.
    #
    # Returns:
    #    (int): The size of the cache.
    #
    def calculate_cache_size(self):
        return utils._get_dir_size(self.casdir)

    # list_refs():
    #
    # List refs in Least Recently Modified (LRM) order.
    #
    # Returns:
    #     (list) - A list of refs in LRM order
    #
    def list_refs(self):
        # string of: /path/to/repo/refs/heads
        ref_heads = os.path.join(self.casdir, 'refs', 'heads')

        refs = []
        mtimes = []

        for root, _, files in os.walk(ref_heads):
            for filename in files:
                ref_path = os.path.join(root, filename)
                refs.append(os.path.relpath(ref_path, ref_heads))
                # Obtain the mtime (the time a file was last modified)
                mtimes.append(os.path.getmtime(ref_path))

        # NOTE: Sorted will sort from earliest to latest, thus the
        # first ref of this list will be the file modified earliest.
        return [ref for _, ref in sorted(zip(mtimes, refs))]

    # list_objects():
    #
    # List cached objects in Least Recently Modified (LRM) order.
    #
    # Returns:
    #     (list) - A list of objects and timestamps in LRM order
    #
    def list_objects(self):
        objs = []
        mtimes = []

        for root, _, files in os.walk(os.path.join(self.casdir, 'objects')):
            for filename in files:
                obj_path = os.path.join(root, filename)
                try:
                    mtimes.append(os.path.getmtime(obj_path))
                except FileNotFoundError:
                    pass
                else:
                    objs.append(obj_path)

        # NOTE: Sorted will sort from earliest to latest, thus the
        # first element of this list will be the file modified earliest.
        return sorted(zip(mtimes, objs))

    def clean_up_refs_until(self, time):
        ref_heads = os.path.join(self.casdir, 'refs', 'heads')

        for root, _, files in os.walk(ref_heads):
            for filename in files:
                ref_path = os.path.join(root, filename)
                # Obtain the mtime (the time a file was last modified)
                if os.path.getmtime(ref_path) < time:
                    os.unlink(ref_path)

    # remove():
    #
    # Removes the given symbolic ref from the repo.
    #
    # Args:
    #    ref (str): A symbolic ref
    #    defer_prune (bool): Whether to defer pruning to the caller. NOTE:
    #                        The space won't be freed until you manually
    #                        call prune.
    #
    # Returns:
    #    (int|None) The amount of space pruned from the repository in
    #               Bytes, or None if defer_prune is True
    #
    def remove(self, ref, *, defer_prune=False):

        # Remove cache ref
        refpath = self._refpath(ref)
        if not os.path.exists(refpath):
            raise CASCacheError("Could not find ref '{}'".format(ref))

        os.unlink(refpath)

        if not defer_prune:
            pruned = self.prune()
            return pruned

        return None

    # prune():
    #
    # Prune unreachable objects from the repo.
    #
    def prune(self):
        ref_heads = os.path.join(self.casdir, 'refs', 'heads')

        pruned = 0
        reachable = set()

        # Check which objects are reachable
        for root, _, files in os.walk(ref_heads):
            for filename in files:
                ref_path = os.path.join(root, filename)
                ref = os.path.relpath(ref_path, ref_heads)

                tree = self.resolve_ref(ref)
                self._reachable_refs_dir(reachable, tree)

        # Prune unreachable objects
        for root, _, files in os.walk(os.path.join(self.casdir, 'objects')):
            for filename in files:
                objhash = os.path.basename(root) + filename
                if objhash not in reachable:
                    obj_path = os.path.join(root, filename)
                    pruned += os.stat(obj_path).st_size
                    os.unlink(obj_path)

        return pruned

    def update_tree_mtime(self, tree):
        reachable = set()
        self._reachable_refs_dir(reachable, tree, update_mtime=True)

    ################################################
    #             Local Private Methods            #
    ################################################

    def _checkout(self, dest, tree):
        os.makedirs(dest, exist_ok=True)

        directory = remote_execution_pb2.Directory()

        with open(self.objpath(tree), 'rb') as f:
            directory.ParseFromString(f.read())

        for filenode in directory.files:
            # regular file, create hardlink
            fullpath = os.path.join(dest, filenode.name)
            os.link(self.objpath(filenode.digest), fullpath)

            if filenode.is_executable:
                os.chmod(fullpath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR |
                         stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        for dirnode in directory.directories:
            # Don't try to checkout a dangling ref
            if os.path.exists(self.objpath(dirnode.digest)):
                fullpath = os.path.join(dest, dirnode.name)
                self._checkout(fullpath, dirnode.digest)

        for symlinknode in directory.symlinks:
            # symlink
            fullpath = os.path.join(dest, symlinknode.name)
            os.symlink(symlinknode.target, fullpath)

    def _refpath(self, ref):
        return os.path.join(self.casdir, 'refs', 'heads', ref)

    # _commit_directory():
    #
    # Adds local directory to content addressable store.
    #
    # Adds files, symbolic links and recursively other directories in
    # a local directory to the content addressable store.
    #
    # Args:
    #     path (str): Path to the directory to add.
    #     dir_digest (Digest): An optional Digest object to use.
    #
    # Returns:
    #     (Digest): Digest object for the directory added.
    #
    def _commit_directory(self, path, *, dir_digest=None):
        directory = remote_execution_pb2.Directory()

        for name in sorted(os.listdir(path)):
            full_path = os.path.join(path, name)
            mode = os.lstat(full_path).st_mode
            if stat.S_ISDIR(mode):
                dirnode = directory.directories.add()
                dirnode.name = name
                self._commit_directory(full_path, dir_digest=dirnode.digest)
            elif stat.S_ISREG(mode):
                filenode = directory.files.add()
                filenode.name = name
                self.add_object(path=full_path, digest=filenode.digest)
                filenode.is_executable = (mode & stat.S_IXUSR) == stat.S_IXUSR
            elif stat.S_ISLNK(mode):
                symlinknode = directory.symlinks.add()
                symlinknode.name = name
                symlinknode.target = os.readlink(full_path)
            elif stat.S_ISSOCK(mode):
                # The process serving the socket can't be cached anyway
                pass
            else:
                raise CASCacheError("Unsupported file type for {}".format(full_path))

        return self.add_object(digest=dir_digest,
                               buffer=directory.SerializeToString())

    def _get_subdir(self, tree, subdir):
        head, name = os.path.split(subdir)
        if head:
            tree = self._get_subdir(tree, head)

        directory = remote_execution_pb2.Directory()

        with open(self.objpath(tree), 'rb') as f:
            directory.ParseFromString(f.read())

        for dirnode in directory.directories:
            if dirnode.name == name:
                return dirnode.digest

        raise CASCacheError("Subdirectory {} not found".format(name))

    def _diff_trees(self, tree_a, tree_b, *, added, removed, modified, path=""):
        dir_a = remote_execution_pb2.Directory()
        dir_b = remote_execution_pb2.Directory()

        if tree_a:
            with open(self.objpath(tree_a), 'rb') as f:
                dir_a.ParseFromString(f.read())
        if tree_b:
            with open(self.objpath(tree_b), 'rb') as f:
                dir_b.ParseFromString(f.read())

        a = 0
        b = 0
        while a < len(dir_a.files) or b < len(dir_b.files):
            if b < len(dir_b.files) and (a >= len(dir_a.files) or
                                         dir_a.files[a].name > dir_b.files[b].name):
                added.append(os.path.join(path, dir_b.files[b].name))
                b += 1
            elif a < len(dir_a.files) and (b >= len(dir_b.files) or
                                           dir_b.files[b].name > dir_a.files[a].name):
                removed.append(os.path.join(path, dir_a.files[a].name))
                a += 1
            else:
                # File exists in both directories
                if dir_a.files[a].digest.hash != dir_b.files[b].digest.hash:
                    modified.append(os.path.join(path, dir_a.files[a].name))
                a += 1
                b += 1

        a = 0
        b = 0
        while a < len(dir_a.directories) or b < len(dir_b.directories):
            if b < len(dir_b.directories) and (a >= len(dir_a.directories) or
                                               dir_a.directories[a].name > dir_b.directories[b].name):
                self._diff_trees(None, dir_b.directories[b].digest,
                                 added=added, removed=removed, modified=modified,
                                 path=os.path.join(path, dir_b.directories[b].name))
                b += 1
            elif a < len(dir_a.directories) and (b >= len(dir_b.directories) or
                                                 dir_b.directories[b].name > dir_a.directories[a].name):
                self._diff_trees(dir_a.directories[a].digest, None,
                                 added=added, removed=removed, modified=modified,
                                 path=os.path.join(path, dir_a.directories[a].name))
                a += 1
            else:
                # Subdirectory exists in both directories
                if dir_a.directories[a].digest.hash != dir_b.directories[b].digest.hash:
                    self._diff_trees(dir_a.directories[a].digest, dir_b.directories[b].digest,
                                     added=added, removed=removed, modified=modified,
                                     path=os.path.join(path, dir_a.directories[a].name))
                a += 1
                b += 1

    def _reachable_refs_dir(self, reachable, tree, update_mtime=False):
        if tree.hash in reachable:
            return

        if update_mtime:
            os.utime(self.objpath(tree))

        reachable.add(tree.hash)

        directory = remote_execution_pb2.Directory()

        with open(self.objpath(tree), 'rb') as f:
            directory.ParseFromString(f.read())

        for filenode in directory.files:
            if update_mtime:
                os.utime(self.objpath(filenode.digest))
            reachable.add(filenode.digest.hash)

        for dirnode in directory.directories:
            self._reachable_refs_dir(reachable, dirnode.digest, update_mtime=update_mtime)

    def _required_blobs(self, directory_digest):
        # parse directory, and recursively add blobs
        d = remote_execution_pb2.Digest()
        d.hash = directory_digest.hash
        d.size_bytes = directory_digest.size_bytes
        yield d

        directory = remote_execution_pb2.Directory()

        with open(self.objpath(directory_digest), 'rb') as f:
            directory.ParseFromString(f.read())

        for filenode in directory.files:
            d = remote_execution_pb2.Digest()
            d.hash = filenode.digest.hash
            d.size_bytes = filenode.digest.size_bytes
            yield d

        for dirnode in directory.directories:
            yield from self._required_blobs(dirnode.digest)

    # _ensure_blob():
    #
    # Fetch and add blob if it's not already local.
    #
    # Args:
    #     remote (Remote): The remote to use.
    #     digest (Digest): Digest object for the blob to fetch.
    #
    # Returns:
    #     (str): The path of the object
    #
    def _ensure_blob(self, remote, digest):
        objpath = self.objpath(digest)
        if os.path.exists(objpath):
            # already in local repository
            return objpath

        with tempfile.NamedTemporaryFile(dir=self.tmpdir) as f:
            remote._fetch_blob(digest, f)

            added_digest = self.add_object(path=f.name, link_directly=True)
            assert added_digest.hash == digest.hash

        return objpath

    def _batch_download_complete(self, batch):
        for digest, data in batch.send():
            with tempfile.NamedTemporaryFile(dir=self.tmpdir) as f:
                f.write(data)
                f.flush()

                added_digest = self.add_object(path=f.name, link_directly=True)
                assert added_digest.hash == digest.hash

    # Helper function for _fetch_directory().
    def _fetch_directory_batch(self, remote, batch, fetch_queue, fetch_next_queue):
        self._batch_download_complete(batch)

        # All previously scheduled directories are now locally available,
        # move them to the processing queue.
        fetch_queue.extend(fetch_next_queue)
        fetch_next_queue.clear()
        return _CASBatchRead(remote)

    # Helper function for _fetch_directory().
    def _fetch_directory_node(self, remote, digest, batch, fetch_queue, fetch_next_queue, *, recursive=False):
        in_local_cache = os.path.exists(self.objpath(digest))

        if in_local_cache:
            # Skip download, already in local cache.
            pass
        elif (digest.size_bytes >= remote.max_batch_total_size_bytes or
              not remote.batch_read_supported):
            # Too large for batch request, download in independent request.
            self._ensure_blob(remote, digest)
            in_local_cache = True
        else:
            if not batch.add(digest):
                # Not enough space left in batch request.
                # Complete pending batch first.
                batch = self._fetch_directory_batch(remote, batch, fetch_queue, fetch_next_queue)
                batch.add(digest)

        if recursive:
            if in_local_cache:
                # Add directory to processing queue.
                fetch_queue.append(digest)
            else:
                # Directory will be available after completing pending batch.
                # Add directory to deferred processing queue.
                fetch_next_queue.append(digest)

        return batch

    # _fetch_directory():
    #
    # Fetches remote directory and adds it to content addressable store.
    #
    # Fetches files, symbolic links and recursively other directories in
    # the remote directory and adds them to the content addressable
    # store.
    #
    # Args:
    #     remote (Remote): The remote to use.
    #     dir_digest (Digest): Digest object for the directory to fetch.
    #     excluded_subdirs (list): The optional list of subdirs to not fetch
    #
    def _fetch_directory(self, remote, dir_digest, *, excluded_subdirs=None):
        fetch_queue = [dir_digest]
        fetch_next_queue = []
        batch = _CASBatchRead(remote)
        if not excluded_subdirs:
            excluded_subdirs = []

        while len(fetch_queue) + len(fetch_next_queue) > 0:
            if not fetch_queue:
                batch = self._fetch_directory_batch(remote, batch, fetch_queue, fetch_next_queue)

            dir_digest = fetch_queue.pop(0)

            objpath = self._ensure_blob(remote, dir_digest)

            directory = remote_execution_pb2.Directory()
            with open(objpath, 'rb') as f:
                directory.ParseFromString(f.read())

            for dirnode in directory.directories:
                if dirnode.name not in excluded_subdirs:
                    batch = self._fetch_directory_node(remote, dirnode.digest, batch,
                                                       fetch_queue, fetch_next_queue, recursive=True)

            for filenode in directory.files:
                batch = self._fetch_directory_node(remote, filenode.digest, batch,
                                                   fetch_queue, fetch_next_queue)

        # Fetch final batch
        self._fetch_directory_batch(remote, batch, fetch_queue, fetch_next_queue)

    def _fetch_subdir(self, remote, tree, subdir):
        subdirdigest = self._get_subdir(tree, subdir)
        self._fetch_directory(remote, subdirdigest)

    def _fetch_tree(self, remote, digest):
        # download but do not store the Tree object
        with tempfile.NamedTemporaryFile(dir=self.tmpdir) as out:
            remote._fetch_blob(digest, out)

            tree = remote_execution_pb2.Tree()

            with open(out.name, 'rb') as f:
                tree.ParseFromString(f.read())

            tree.children.extend([tree.root])
            for directory in tree.children:
                for filenode in directory.files:
                    self._ensure_blob(remote, filenode.digest)

                # place directory blob only in final location when we've downloaded
                # all referenced blobs to avoid dangling references in the repository
                dirbuffer = directory.SerializeToString()
                dirdigest = self.add_object(buffer=dirbuffer)
                assert dirdigest.size_bytes == len(dirbuffer)

        return dirdigest

    def _send_directory(self, remote, digest, u_uid=uuid.uuid4()):
        required_blobs = self._required_blobs(digest)

        missing_blobs = dict()
        # Limit size of FindMissingBlobs request
        for required_blobs_group in _grouper(required_blobs, 512):
            request = remote_execution_pb2.FindMissingBlobsRequest(instance_name=remote.spec.instance_name)

            for required_digest in required_blobs_group:
                d = request.blob_digests.add()
                d.hash = required_digest.hash
                d.size_bytes = required_digest.size_bytes

            response = remote.cas.FindMissingBlobs(request)
            for missing_digest in response.missing_blob_digests:
                d = remote_execution_pb2.Digest()
                d.hash = missing_digest.hash
                d.size_bytes = missing_digest.size_bytes
                missing_blobs[d.hash] = d

        # Upload any blobs missing on the server
        self._send_blobs(remote, missing_blobs.values(), u_uid)

    def _send_blobs(self, remote, digests, u_uid=uuid.uuid4()):
        batch = _CASBatchUpdate(remote)

        for digest in digests:
            with open(self.objpath(digest), 'rb') as f:
                assert os.fstat(f.fileno()).st_size == digest.size_bytes

                if (digest.size_bytes >= remote.max_batch_total_size_bytes or
                        not remote.batch_update_supported):
                    # Too large for batch request, upload in independent request.
                    remote._send_blob(digest, f, u_uid=u_uid)
                else:
                    if not batch.add(digest, f):
                        # Not enough space left in batch request.
                        # Complete pending batch first.
                        batch.send()
                        batch = _CASBatchUpdate(remote)
                        batch.add(digest, f)

        # Send final batch
        batch.send()


def _grouper(iterable, n):
    while True:
        try:
            current = next(iterable)
        except StopIteration:
            return
        yield itertools.chain([current], itertools.islice(iterable, n - 1))
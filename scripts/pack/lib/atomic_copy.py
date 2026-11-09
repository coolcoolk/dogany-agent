#!/usr/bin/env python3
"""Pack whole-file delivery with atomic_write.sh's atomic_cp semantics.

Keep native cp (including platform metadata and error behavior), but perform
path, mode, temporary-file and rename operations in one process rather than
starting dirname/basename/stat/mktemp/chmod/mv for every payload file.
"""
import os
import stat
import subprocess
import sys
import tempfile


def copy(src, dst):
    temporary = None
    try:
        if not os.path.isfile(src):
            raise OSError('source is not a regular file: ' + src)
        # _ct_deliver created only the declared destination's parent. Do not
        # create missing parents behind a dangling destination symlink.
        os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
        real = dst
        hops = 0
        while os.path.islink(real):
            hops += 1
            if hops > 40:
                raise OSError('destination symlink limit exceeded: ' + dst)
            link = os.readlink(real)
            real = link if os.path.isabs(link) else os.path.join(os.path.dirname(real), link)
        if os.path.exists(real):
            if not os.path.isfile(real):
                raise OSError('destination is not a regular file: ' + real)
            mode = stat.S_IMODE(os.stat(real).st_mode)
            # atomic_write.sh uses BSD stat's %Lp on macOS: only the
            # user/group/other bits, excluding setuid, setgid and sticky.
            # GNU stat's %a includes those bits, so keep them elsewhere.
            if sys.platform == 'darwin':
                mode &= 0o777
        else:
            mask = os.umask(0)
            os.umask(mask)
            mode = stat.S_IMODE(os.stat(src).st_mode) & ~mask & 0o777
        # Keep the temporary name short even for a maximum-length target.
        # mkstemp adds eight random characters (the old shell added six).
        fd, temporary = tempfile.mkstemp(prefix='.c.aw.',
                                         dir=os.path.dirname(real) or '.')
        os.close(fd)
        # Filling a 0600 sibling first also works for read-only final modes.
        if subprocess.run(['cp', src, temporary]).returncode:
            return 1
        os.chmod(temporary, mode)
        os.replace(temporary, real)
        temporary = None
        return 0
    except OSError as exc:
        print('atomic_copy: %s' % exc, file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


if __name__ == '__main__':
    sys.exit(copy(*sys.argv[1:]))

"""跨平台文件锁（Unix: fcntl.flock; Windows: msvcrt.locking）。"""

import os
import sys


def lock_file_ex_nb(fp) -> None:
    """非阻塞独占锁。如果被占用，抛出 BlockingIOError。"""
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fp.fileno(), 0, os.SEEK_SET)
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        except (OSError, IOError, PermissionError) as e:
            raise BlockingIOError(str(e)) from e
    else:
        import fcntl

        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_file(fp) -> None:
    """释放锁。"""
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fp.fileno(), 0, os.SEEK_SET)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        except (OSError, IOError):
            pass
    else:
        import fcntl

        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

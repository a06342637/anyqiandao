import os


class WorkerLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        self.handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.handle.write(b'0')
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise RuntimeError('已有队列执行器占用此数据库，只允许单实例、单 worker 运行') from None

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None

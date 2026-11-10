"""Stand-ins for the mail server's shared folder and for a computer's clock."""
import secrets

from paperbot.sync import Entry


class FakeBox:
    """The shared folder on the "mail server": one object for all computers in a test."""

    def __init__(self):
        self.messages = None            # {uid: (Entry, payload)}; None = no folder yet
        self.next_uid = 1
        self.now = 1_700_000_000.0      # the server's clock
        self.down = False               # True: the server can't be reached
        self.saved = 0                  # how many messages were ever saved

    def kinds(self):
        return sorted(e.kind for e, _ in (self.messages or {}).values())


class FakeStore:
    def __init__(self, box: FakeBox):
        self.box = box

    def _reach(self):
        if self.box.down:
            raise OSError("mail server unreachable")

    def entries(self):
        self._reach()
        if self.box.messages is None:
            return None
        return [e for _, (e, _) in sorted(self.box.messages.items())]

    def add(self, kind, *, session="", node="", version=0, digest="", payload=None):
        self._reach()
        box = self.box
        if box.messages is None:
            box.messages = {}
        ident = secrets.token_hex(8)
        box.messages[box.next_uid] = (Entry(box.next_uid, box.now, kind, ident, session, node,
                                            version, digest), payload)
        box.next_uid += 1
        box.saved += 1
        return ident

    def payload(self, uid):
        self._reach()
        return self.box.messages[uid][1]

    def remove(self, uids):
        self._reach()
        for uid in uids:
            (self.box.messages or {}).pop(uid, None)

    def clone(self):
        return FakeStore(self.box)

    def close(self):
        pass


class FakeClock:
    """One computer's clock. Sleeping lets time pass for everybody (the server included)."""

    def __init__(self, box: FakeBox, wrong_by: float = 0.0):
        self.box = box
        self.wrong_by = wrong_by        # this computer's clock is off by this much
        self.asleep = 0.0               # time this computer spent suspended
        self.during_sleep = None        # call once inside the next sleep(): "meanwhile, ..."

    def time(self):
        return self.box.now + self.wrong_by

    def monotonic(self):
        return self.box.now - self.asleep

    def sleep(self, seconds):
        meanwhile, self.during_sleep = self.during_sleep, None
        if meanwhile:
            meanwhile()
        self.box.now += seconds

    def suspend(self, seconds):
        """The lid was closed: the wall clock moves on, the computer's own timer doesn't."""
        self.box.now += seconds
        self.asleep += seconds

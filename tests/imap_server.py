"""A small IMAP server for tests: just enough of the protocol for the folder PaperBot keeps
its shared records in (paperbot.sync.ImapStore), spoken over a real socket to the real
imaplib. Two flavours:

  * plain  - a folder holds messages; deleting deletes (Dovecot and most servers).
  * gmail  - folders are labels on one pile of mail. Deleting from a folder only takes the
             label off: the message stays in "[Gmail]/All Mail". Only moving it to the Bin and
             expunging it there removes it. That is what ImapStore's Bin handling is for, and
             the reason these tests check that nothing else in the Bin is ever touched.
"""
from __future__ import annotations

import re
import socket
import socketserver
import threading
import time
from email import message_from_bytes
from email.utils import parsedate_to_datetime

USER, PASSWORD = "paperbot@test.invalid", "app password"
ALL_MAIL, BIN = "[Gmail]/All Mail", "[Gmail]/Bin"
TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\([^)]*\)|\S+')


class Mail:
    """One stored message. Under Gmail the same one can carry several labels."""

    def __init__(self, raw: bytes, when: float):
        self.raw, self.when = raw, when

    @property
    def subject(self) -> str:
        return str(message_from_bytes(self.raw).get("Subject", ""))


class Folder:
    def __init__(self, validity: int):
        self.validity, self.next_uid = validity, 1
        self.mails: dict[int, Mail] = {}          # uid -> message
        self.deleted: set[int] = set()            # uids flagged \Deleted

    def add(self, mail: Mail) -> int:
        uid, self.next_uid = self.next_uid, self.next_uid + 1
        self.mails[uid] = mail
        return uid

    def pick(self, spec: str) -> list[int]:
        """'1:*', '4', '4,7', '4:6' -> the uids that exist."""
        top = max(self.mails, default=0)
        wanted: set[int] = set()
        for part in spec.split(","):
            lo, _, hi = part.partition(":")
            a = top if lo == "*" else int(lo)
            b = a if not hi else (top if hi == "*" else int(hi))
            wanted.update(u for u in self.mails if min(a, b) <= u <= max(a, b))
        return sorted(wanted)

    def seq(self, uid: int) -> int:
        return sorted(self.mails).index(uid) + 1


def unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] == '"':
        return re.sub(r"\\(.)", r"\1", token[1:-1])
    return token


def header_fields(raw: bytes, names: list[str]) -> bytes:
    """What BODY[HEADER.FIELDS (...)] returns: the named header lines and a blank line."""
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    wanted = {n.lower().encode() for n in names}
    out, keep = [], False
    for line in head:
        if line[:1] not in (b" ", b"\t"):
            keep = line.split(b":", 1)[0].strip().lower() in wanted
        if keep:
            out.append(line)
    return b"\r\n".join(out) + b"\r\n\r\n" if out else b"\r\n"


def imap_time(when: float) -> str:
    return time.strftime("%d-%b-%Y %H:%M:%S +0000", time.gmtime(when))


class ImapServer:
    """gmail: labels, All Mail and a Bin.  bin_visible=False: the owner hid the Bin from IMAP.
    dates_from_header: a server that stamps arrivals with the message's own Date instead of
    its clock.  uid_last: put the UID after the message text in FETCH answers (servers differ).
    caps: what the server says it can do."""

    def __init__(self, gmail: bool = False, bin_visible: bool = True,
                 dates_from_header: bool = False, uid_last: bool = False,
                 caps: str = "IMAP4rev1 UIDPLUS MOVE"):
        self.gmail, self.bin_visible = gmail, bin_visible
        self.dates_from_header, self.uid_last = dates_from_header, uid_last
        self.caps = caps + (" X-GM-EXT-1" if gmail else "")
        self.clock = time.time
        self.lock = threading.RLock()
        self.folders: dict[str, Folder] = {}
        self.commands: list[str] = []             # every command received, in order
        self.logins = 0
        self._validity = 100
        self._sockets: set[socket.socket] = set()
        for name in ["INBOX"] + ([ALL_MAIL, BIN] if gmail else []):
            self.create(name)
        self._tcp = _Tcp(("127.0.0.1", 0), _Handler)
        self._tcp.imap = self
        self.port = self._tcp.server_address[1]
        threading.Thread(target=self._tcp.serve_forever, daemon=True).start()

    # ---- for the tests -------------------------------------------------------------------
    def stop(self) -> None:
        self._tcp.shutdown()
        self._tcp.server_close()
        self.drop_connections()

    def drop_connections(self) -> None:
        """Every connection dies, as when the network blips."""
        for s in list(self._sockets):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def subjects(self, folder: str) -> list[str]:
        with self.lock:
            f = self.folders.get(folder)
            return [m.subject for _, m in sorted(f.mails.items())] if f else []

    def put(self, folder: str, subject: str, sender: str = "someone@else.example",
            body: str = "hello") -> Mail:
        """Some other mail of the owner's, straight into a folder."""
        raw = (f"From: {sender}\r\nTo: {USER}\r\nSubject: {subject}\r\n"
               f"Message-ID: <{len(self.commands)}.{time.monotonic_ns()}@else.example>\r\n\r\n"
               f"{body}\r\n").encode()
        with self.lock:
            mail = Mail(raw, self.clock())
            self.folders[folder].add(mail)
            if self.gmail and folder not in (ALL_MAIL, BIN):
                self.folders[ALL_MAIL].add(mail)
            return mail

    # ---- what the commands do ---------------------------------------------------------------
    def create(self, name: str) -> None:
        if name not in self.folders:
            self._validity += 1
            self.folders[name] = Folder(self._validity)

    def append(self, name: str, raw: bytes) -> int | None:
        folder = self.folders.get(name)
        if folder is None:
            return None
        when = float(int(self.clock()))           # mail servers keep whole seconds
        if self.dates_from_header:
            try:
                when = parsedate_to_datetime(str(message_from_bytes(raw)["Date"])).timestamp()
            except (TypeError, ValueError):
                pass
        mail = Mail(raw, when)
        uid = folder.add(mail)
        if self.gmail and name not in (ALL_MAIL, BIN):
            self.folders[ALL_MAIL].add(mail)
        return uid

    def expunge(self, name: str, uids: list[int]) -> list[int]:
        """Returns the sequence numbers that went, highest first."""
        folder = self.folders[name]
        gone = sorted((u for u in uids if u in folder.deleted and u in folder.mails),
                      reverse=True)
        seqs = [folder.seq(u) for u in gone]
        for u in gone:
            del folder.mails[u]                   # gmail: a label came off; All Mail keeps it
            folder.deleted.discard(u)
        return seqs

    def transfer(self, src: str, uids: list[int], dst: str, move: bool):
        """COPY or MOVE. Returns (source uids, new uids, sequence numbers that left)."""
        source, target = self.folders[src], self.folders[dst]
        picked = [(u, source.mails[u]) for u in sorted(uids) if u in source.mails]
        binned = self.gmail and dst == BIN        # Gmail: even "copied" to the Bin means binned
        seqs = sorted((source.seq(u) for u, _ in picked), reverse=True) if move or binned else []
        new = []
        for u, mail in picked:
            if binned:                            # off every label and out of All Mail
                for name, folder in self.folders.items():
                    if name != BIN:
                        for other in [k for k, m in folder.mails.items() if m is mail]:
                            del folder.mails[other]
                            folder.deleted.discard(other)
            elif move:
                del source.mails[u]
                source.deleted.discard(u)
            new.append(target.add(mail))
        return [u for u, _ in picked], new, seqs


class _Tcp(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    imap: ImapServer


class _Handler(socketserver.StreamRequestHandler):
    def send(self, *lines: str) -> None:
        self.wfile.write(b"".join(line.encode("utf-8") + b"\r\n" for line in lines))
        self.wfile.flush()

    def handle(self) -> None:
        srv = self.server.imap
        srv._sockets.add(self.connection)
        self.selected: str | None = None
        self.user: str | None = None
        try:
            self.send("* OK IMAP4rev1 PaperBot test server ready")
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                tag, _, rest = line.rstrip(b"\r\n").decode("utf-8", "replace").partition(" ")
                with srv.lock:
                    srv.commands.append(re.sub(r'^(LOGIN \S+) .*', r"\1 ***", rest))
                if not self.command(tag, rest):
                    break
        except (OSError, ValueError):
            pass
        finally:
            srv._sockets.discard(self.connection)

    def command(self, tag: str, rest: str) -> bool:
        srv = self.server.imap
        name, _, args = rest.partition(" ")
        name = name.upper()
        by_uid = name == "UID"
        if by_uid:
            name, _, args = args.partition(" ")
            name = name.upper()
        literal = None
        if name == "APPEND":                      # read the message before taking the lock
            tokens = TOKEN.findall(args)
            size = re.search(r"\{(\d+)\}$", args)
            with srv.lock:
                exists = unquote(tokens[0]) in srv.folders
            if not exists or not size:
                self.send(f"{tag} NO [TRYCREATE] Folder doesn't exist. (Failure)")
                return True
            self.send("+ go ahead")
            literal = self.rfile.read(int(size.group(1)))
            self.rfile.readline()                 # the line end after the message
        with srv.lock:
            return self.run(tag, name, args, by_uid, literal)

    def run(self, tag: str, name: str, args: str, by_uid: bool, literal: bytes | None) -> bool:
        srv = self.server.imap
        tokens = TOKEN.findall(args)
        folder = srv.folders.get(self.selected) if self.selected else None
        if name == "CAPABILITY":
            self.send(f"* CAPABILITY {srv.caps}", f"{tag} OK Thats all she wrote!")
        elif name == "NOOP":
            self.send(f"{tag} OK Success")
        elif name == "LOGOUT":
            self.send("* BYE Logging out", f"{tag} OK Bye")
            return False
        elif name == "LOGIN":
            if len(tokens) == 2 and (unquote(tokens[0]), unquote(tokens[1])) == (USER, PASSWORD):
                self.user = USER
                srv.logins += 1
                self.send(f"{tag} OK {USER} authenticated (Success)")
            else:
                self.send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        elif self.user is None:
            self.send(f"{tag} BAD Please log in first")
        elif name in ("SELECT", "EXAMINE"):
            target = unquote(tokens[0])
            hidden = srv.gmail and target == BIN and not srv.bin_visible
            if target not in srv.folders or hidden:
                self.selected = None
                self.send(f"{tag} NO [NONEXISTENT] Unknown Mailbox: {target} (Failure)")
            else:
                self.selected, f = target, srv.folders[target]
                self.send("* FLAGS (\\Answered \\Flagged \\Draft \\Deleted \\Seen)",
                          f"* {len(f.mails)} EXISTS", "* 0 RECENT",
                          f"* OK [UIDVALIDITY {f.validity}] UIDs valid.",
                          f"* OK [UIDNEXT {f.next_uid}] Predicted next UID.",
                          f"{tag} OK [READ-WRITE] {target} selected. (Success)")
        elif name == "LIST":
            pattern = unquote(tokens[1]) if len(tokens) > 1 else "*"
            rx = re.compile("^" + re.escape(pattern).replace(r"\*", ".*").replace("%", "[^/]*")
                            + "$")
            for fname in srv.folders:
                if not rx.match(fname) or (fname == BIN and not srv.bin_visible):
                    continue
                flags = "\\HasNoChildren" + (" \\Trash" if fname == BIN else "") + \
                    (" \\All" if fname == ALL_MAIL else "")
                self.send(f'* LIST ({flags}) "/" "{fname}"')
            self.send(f"{tag} OK Success")
        elif name == "CREATE":
            srv.create(unquote(tokens[0]))
            self.send(f"{tag} OK Success")
        elif name == "DELETE":
            srv.folders.pop(unquote(tokens[0]), None)
            self.send(f"{tag} OK Success")
        elif name == "APPEND":
            uid = srv.append(unquote(tokens[0]), literal or b"")
            f = srv.folders[unquote(tokens[0])]
            self.send(f"{tag} OK [APPENDUID {f.validity} {uid}] (Success)")
        elif folder is None:
            self.send(f"{tag} BAD No folder selected")
        elif name == "SEARCH" and by_uid:           # whatever was asked: everything
            self.send("* SEARCH " + " ".join(map(str, sorted(folder.mails))),
                      f"{tag} OK SEARCH completed (Success)")
        elif name == "FETCH" and by_uid:
            spec, _, items = args.partition(" ")
            self.fetch(folder, folder.pick(spec), items)
            self.send(f"{tag} OK Success")
        elif name == "STORE" and by_uid:
            if tokens[1].startswith("+") and "\\Deleted" in tokens[2]:
                folder.deleted.update(folder.pick(tokens[0]))
            self.send(f"{tag} OK Success")
        elif name == "EXPUNGE":
            if by_uid and "UIDPLUS" not in srv.caps:
                self.send(f"{tag} BAD Unknown command")
                return True
            uids = folder.pick(tokens[0]) if by_uid else sorted(folder.deleted)
            for seq in srv.expunge(self.selected, uids):
                self.send(f"* {seq} EXPUNGE")
            self.send(f"{tag} OK Success")
        elif name in ("MOVE", "COPY") and by_uid:
            target = unquote(tokens[1])
            hidden = srv.gmail and target == BIN and not srv.bin_visible
            if name == "MOVE" and "MOVE" not in srv.caps:
                self.send(f"{tag} BAD Unknown command")
            elif target not in srv.folders or hidden:
                self.send(f"{tag} NO [TRYCREATE] No folder {target} (Failure)")
            else:
                took, new, seqs = srv.transfer(self.selected, folder.pick(tokens[0]), target,
                                               name == "MOVE")
                code = (f"[COPYUID {srv.folders[target].validity} "
                        f"{','.join(map(str, took))} {','.join(map(str, new))}] ") if took else ""
                if name == "MOVE":
                    self.send(*([f"* OK {code}(Success)"] if took else []),
                              *[f"* {seq} EXPUNGE" for seq in seqs], f"{tag} OK Success")
                else:
                    self.send(*[f"* {seq} EXPUNGE" for seq in seqs],
                              f"{tag} OK {code}(Success)")
        else:
            self.send(f"{tag} BAD Unknown command {name}")
        return True

    def fetch(self, folder: Folder, uids: list[int], items: str) -> None:
        srv = self.server.imap
        fields = re.search(r"HEADER\.FIELDS \(([^)]*)\)", items, re.I)
        for uid in uids:
            mail = folder.mails[uid]
            parts = [f'INTERNALDATE "{imap_time(mail.when)}"'] if "INTERNALDATE" in items else []
            if fields:
                label = f"BODY[HEADER.FIELDS ({fields.group(1)})]"
                data = header_fields(mail.raw, fields.group(1).split())
            elif "[]" in items:
                label, data = "BODY[]", mail.raw
            else:
                self.send(f"* {folder.seq(uid)} FETCH ({' '.join([f'UID {uid}'] + parts)})")
                continue
            before = parts + [label] if srv.uid_last else [f"UID {uid}"] + parts + [label]
            after = f" UID {uid})" if srv.uid_last else ")"
            self.wfile.write(f"* {folder.seq(uid)} FETCH ({' '.join(before)} "
                             f"{{{len(data)}}}\r\n".encode() + data + after.encode() + b"\r\n")
        self.wfile.flush()

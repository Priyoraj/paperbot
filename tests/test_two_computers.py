"""Two PaperBots - the office PC and a laptop - answering the same mailbox together."""
import pytest

from fakes import FakeBox, FakeClock, FakeStore
from paperbot.config import Config
from paperbot.db import DB, state_digest
from paperbot.pipeline import Bot
from paperbot.publisher import FetchResult
from paperbot.sources import Candidate
from paperbot.sync import LEASE_S, Busy, LockLost, Sync
from test_pipeline import (OWNER, PDF, FakeMail, FakeOA, FakePublisher, FakeResolver, FakeVpn,
                           body_of, custom_limits, email_msg, owner_reply)

PAPERS = {
    "10.1000/oa": dict(doi="10.1000/oa", title="Open paper", authors="Lee et al.", year=2020),
    "10.1000/captcha": dict(doi="10.1000/captcha", title="Guarded paper", year=2019),
    **{f"10.1000/pay{n}": dict(doi=f"10.1000/pay{n}", title=f"Paywalled paper {n}", year=2021)
       for n in (1, 2, 3, 4)},
}
OPEN = {"10.1000/oa": [Candidate("https://good/oa.pdf", "publishedVersion", "unpaywall")]}


class PowerCut(BaseException):
    """The computer went off: nothing after this runs properly."""


class Publisher(FakePublisher):
    during_fetch = None                 # "while this computer is downloading, ..."

    def fetch(self, paper):
        meanwhile, self.during_fetch = self.during_fetch, None
        if meanwhile:
            meanwhile()
        return super().fetch(paper)


class Manual:
    """The owner at the browser window, downloading the PDF by hand."""

    def __init__(self, computer, meanwhile=None):
        self.computer, self.meanwhile = computer, meanwhile

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def capture(self, paper, url):
        assert self.computer.vpn.connected
        if self.meanwhile:
            self.meanwhile()
        return PDF


class Computer:
    def __init__(self, tmp_path, name, mail, box, vpn_enabled=True):
        self.name, self.mail, self.box = name, mail, box
        self.cfg = Config(owner_name="Priya", owner_email=OWNER, mail_username=OWNER,
                          path=tmp_path / name / "config.toml", vpn_enabled=vpn_enabled)
        self.db = DB(self.cfg.data_dir / "paperbot.db")
        self.vpn = FakeVpn()
        results = {doi: FetchResult("ok", PDF) for doi in PAPERS if "pay" in doi}
        results["10.1000/captcha"] = FetchResult("blocked", reason="captcha")
        self.pub = Publisher(results, self.vpn)
        self.clock = FakeClock(box)
        self.sync = Sync(self.db, FakeStore(box), name, self.cfg.data_dir / "sync.lock",
                         vpn=vpn_enabled, clock=self.clock, heartbeat=False)

    def bot(self):
        return Bot(self.cfg, self.db, self.mail, FakeResolver(PAPERS), FakeOA(OPEN), self.vpn,
                   lambda: self.pub, sync=self.sync)

    def run(self, wait_s=0):
        self.bot().run_once(wait_s)


@pytest.fixture
def pair(tmp_path):
    """The PC shares its records; the laptop has joined. Asha is an approved colleague."""
    mail, box = FakeMail(), FakeBox()
    pc, laptop = (Computer(tmp_path, "OFFICE-PC", mail, box),
                  Computer(tmp_path, "LAPTOP", mail, box))
    pc.db.add_person("asha@uni.edu", "Asha", "approved")
    pc.sync.start()
    laptop.run()
    assert laptop.sync.joined and laptop.db.person("asha@uni.edu")
    return pc, laptop


def ask(mail, *papers, frm="Asha <asha@uni.edu>"):
    mail.inbox.append(email_msg(frm, "Paper request", "\n".join(papers)))


def in_step(*computers):
    for c in computers:
        c.sync.refresh()
    return len({state_digest(c.db.export_state()) for c in computers}) == 1


def test_each_request_is_answered_once(pair):
    pc, laptop = pair
    mail = pc.mail
    ask(mail, "10.1000/oa", "10.1000/pay1")
    pc.run()
    laptop.run()
    assert len(mail.to("asha@uni.edu")) == 1
    ask(mail, "10.1000/pay2")
    laptop.run()                                          # this time the laptop is first
    pc.run()
    replies = mail.to("asha@uni.edu")
    assert len(replies) == 2 and len(list(replies[1].iter_attachments())) == 1
    assert pc.pub.fetched == ["10.1000/pay1"] and laptop.pub.fetched == ["10.1000/pay2"]
    assert in_step(pc, laptop)
    for c in pair:                                        # every screen shows the same
        assert c.db.sent_today() == 3 and c.db.downloads_today() == 2
        assert [r["status"] for r in c.db.recent_requests()] == ["done", "done"]
    assert pc.box.kinds() == ["state", "state"]           # no lock left lying around


def test_checks_that_find_nothing_write_nothing(pair):
    pc, laptop = pair
    saved = pc.box.saved
    for _ in range(3):
        pc.run()
        laptop.run()
    assert pc.box.saved == saved


def test_both_computers_checking_at_the_same_moment(pair):
    pc, laptop = pair
    ask(pc.mail, "10.1000/pay1")

    def laptop_checks_too():
        with pytest.raises(Busy, match="PaperBot on OFFICE-PC is working"):
            laptop.run()

    pc.clock.during_sleep = laptop_checks_too             # while the PC is taking its turn
    pc.run()
    laptop.run()
    assert len(pc.mail.to("asha@uni.edu")) == 1 and laptop.pub.fetched == []


def test_only_one_computer_is_on_the_vpn_at_a_time(pair):
    pc, laptop = pair
    ask(pc.mail, "10.1000/pay1")
    ask(pc.mail, "10.1000/pay2")

    def laptop_checks_too():
        assert pc.vpn.connected
        with pytest.raises(Busy):
            laptop.run()
        assert laptop.vpn.connects == 0

    pc.pub.during_fetch = laptop_checks_too
    pc.run()
    assert len(pc.mail.to("asha@uni.edu")) == 2 and laptop.vpn.connects == 0


def test_download_limits_count_for_both_computers_together(pair):
    pc, laptop = pair
    for c in pair:
        custom_limits(c.cfg, day=3, reserve=0, hour=10)
    ask(pc.mail, "10.1000/pay1", "10.1000/pay2")
    pc.run()
    ask(pc.mail, "10.1000/pay3", "10.1000/pay4")
    laptop.run()                                          # only one download is left today
    assert laptop.pub.fetched == ["10.1000/pay3"]
    assert "today's download limit has been reached" in body_of(pc.mail.to("asha@uni.edu")[1])
    assert in_step(pc, laptop) and pc.db.downloads_today() == 3


def test_a_yes_reaches_the_request_whichever_computer_took_it_in(pair):
    pc, laptop = pair
    mail = pc.mail
    ask(mail, "10.1000/oa", frm="Ravi <ravi@else.org>")
    pc.run()                                              # the PC asks the owner
    prompt = [m for m in mail.to(OWNER) if "Approve" in m["Subject"]]
    assert len(prompt) == 1 and mail.to("ravi@else.org") == []
    laptop.run()
    assert len([m for m in mail.to(OWNER) if "Approve" in m["Subject"]]) == 1   # not asked twice
    mail.inbox.append(owner_reply(prompt[0], "yes"))
    laptop.run()                                          # the laptop reads the owner's "yes"
    pc.run()
    assert len(mail.to("ravi@else.org")) == 1
    assert in_step(pc, laptop) and pc.db.person("ravi@else.org")["status"] == "approved"


def test_approve_in_the_app_on_the_other_computer(pair):
    pc, laptop = pair
    ask(pc.mail, "10.1000/oa", frm="Ravi <ravi@else.org>")
    pc.run()
    laptop.sync.refresh()                                 # the laptop's People screen shows Ravi
    assert laptop.db.person("ravi@else.org")["status"] == "pending"
    bot = laptop.bot()
    assert bot.decide("ravi@else.org", approve=True) == 1
    bot.run_once()
    pc.run()
    assert len(pc.mail.to("ravi@else.org")) == 1


def test_a_paper_that_needs_the_owner_can_be_done_on_either_computer(pair):
    pc, laptop = pair
    mail = pc.mail
    ask(mail, "10.1000/captcha")
    pc.run()                                              # blocked: goes to "Needs you"
    assert "will send it separately" in body_of(mail.to("asha@uni.edu")[0])
    laptop.run()
    todo = laptop.db.owner_todo()
    assert [r["doi"] for r in todo] == ["10.1000/captcha"]
    assert len([m for m in mail.to(OWNER) if "need you" in m["Subject"]]) == 1   # told once

    dialled = pc.vpn.connects

    def pc_gets_a_request():                              # ...which must wait: the VPN is in use
        ask(mail, "10.1000/pay1")
        with pytest.raises(Busy):
            pc.run()
        assert pc.vpn.connects == dialled

    got = laptop.bot().run_manual(lambda: Manual(laptop, pc_gets_a_request),
                                  say=lambda *a: None, item_ids=[todo[0]["id"]])
    assert got == 1 and len(mail.to("asha@uni.edu")) == 2
    pc.run()
    assert len(mail.to("asha@uni.edu")) == 3 and pc.db.owner_todo() == []
    assert in_step(pc, laptop) and pc.db.downloads_today() == 2   # counted, whoever fetched


def test_a_computer_that_goes_off_mid_run_is_covered_by_the_other(pair):
    pc, laptop = pair
    mail, box = pc.mail, pc.box
    ask(mail, "10.1000/pay1")

    def power_cut():
        box.down = True                                   # it can't even say goodbye
        raise PowerCut()

    pc.pub.during_fetch = power_cut
    with pytest.raises(PowerCut):
        pc.run()
    box.down = False
    with pytest.raises(Busy):
        laptop.run()                                      # the PC still seems to be working
    assert mail.to("asha@uni.edu") == []
    box.now += LEASE_S + 5                                # ...a quarter of an hour later
    laptop.run()
    assert len(mail.to("asha@uni.edu")) == 1 and laptop.pub.fetched == ["10.1000/pay1"]
    pc.run()                                              # the PC is switched on again
    assert len(mail.to("asha@uni.edu")) == 1
    assert in_step(pc, laptop) and pc.db.downloads_today() == 1


def test_a_laptop_that_falls_asleep_mid_run_does_not_reply_a_second_time(pair):
    pc, laptop = pair
    mail = pc.mail
    ask(mail, "10.1000/pay1")

    def lid_closed():
        laptop.clock.suspend(3600)
        pc.run()                                          # the PC carried on meanwhile
        assert len(mail.to("asha@uni.edu")) == 1

    laptop.pub.during_fetch = lid_closed
    with pytest.raises(LockLost):
        laptop.run()                                      # woke up, noticed, stopped
    assert len(mail.to("asha@uni.edu")) == 1
    laptop.run()
    pc.run()
    assert len(mail.to("asha@uni.edu")) == 1
    assert in_step(pc, laptop) and pc.db.downloads_today() == 2   # both downloads happened


def test_a_computer_without_the_vpn_leaves_paywalled_papers_to_the_other(tmp_path):
    mail, box = FakeMail(), FakeBox()
    pc = Computer(tmp_path, "OFFICE-PC", mail, box)
    laptop = Computer(tmp_path, "LAPTOP", mail, box, vpn_enabled=False)
    pc.db.add_person("asha@uni.edu", "Asha", "approved")
    pc.sync.start()
    laptop.run()
    ask(mail, "10.1000/oa", "10.1000/pay1")
    laptop.run()                                          # sends what it can
    first = mail.to("asha@uni.edu")[0]
    assert len(list(first.iter_attachments())) == 1 and "Still working on 1 more" in body_of(first)
    assert laptop.vpn.connects == 0
    pc.run()
    second = mail.to("asha@uni.edu")[1]
    assert len(list(second.iter_attachments())) == 1 and pc.pub.fetched == ["10.1000/pay1"]


def test_without_the_vpn_and_on_its_own_it_says_so_straight_away(tmp_path):
    mail, box = FakeMail(), FakeBox()
    laptop = Computer(tmp_path, "LAPTOP", mail, box, vpn_enabled=False)
    laptop.db.add_person("asha@uni.edu", "Asha", "approved")
    ask(mail, "10.1000/pay1")
    laptop.run()
    assert "institute access isn't available right now" in body_of(mail.to("asha@uni.edu")[0])
    assert box.saved == 0


def test_on_its_own_paperbot_works_as_before_and_writes_nothing_to_the_mailbox(tmp_path):
    mail, box = FakeMail(), FakeBox()
    pc = Computer(tmp_path, "PC", mail, box)
    ask(mail, "10.1000/oa", "10.1000/pay1", frm="Ravi <ravi@else.org>")
    pc.run()
    mail.inbox.append(owner_reply(mail.to(OWNER)[0], "yes"))
    pc.run()
    assert len(list(mail.to("ravi@else.org")[0].iter_attachments())) == 2
    assert box.saved == 0 and box.messages is None


def test_a_paper_picked_just_before_joining_is_never_mistaken_for_another(tmp_path):
    mail, box = FakeMail(), FakeBox()
    pc, laptop = Computer(tmp_path, "PC", mail, box), Computer(tmp_path, "LAPTOP", mail, box)
    for c, who in ((pc, "asha@uni.edu"), (laptop, "ravi@uni.edu")):
        rid = c.db.add_request(f"<1@{c.name}>", who, "", "Paper request", "", "queued")
        c.db.update_item(c.db.add_item(rid, "10.1000/captcha", "doi", "10.1000/captcha"),
                         status="manual", reported=1, owner_notified=1)
    pc.sync.start()
    # On the laptop the owner picks its paper #1 and gives up on it - while the laptop is
    # still on its own. Joining renumbers that paper: #1 is now Asha's.
    assert laptop.bot().give_up([1]) == 0
    assert mail.sent == []                                # nobody got a "sorry" by mistake
    assert [(r["sender"], r["status"]) for r in laptop.db.owner_todo()] == [
        ("asha@uni.edu", "manual"), ("ravi@uni.edu", "manual")]
